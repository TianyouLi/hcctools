# perf script event handlers, generated by perf script -g python
# Licensed under the terms of the GNU GPL License version 2

# The common_* event handler fields are the most useful fields common to
# all events.  They don't necessarily correspond to the 'common_*' fields
# in the format files.  Those fields not available as handler params can
# be retrieved using Python functions of the form common_*(context).
# See the perf-script-python Documentation for the list of available functions.

from __future__ import print_function

import os
import sys
import argparse 
import ipaddress
import pprint
import debugpy as dbg

from typing import List


sys.path.append(os.environ['PERF_EXEC_PATH'] + \
  '/scripts/python/Perf-Trace-Util/lib/Perf/Trace')

from perf_trace_context import *
from Core import *
from EventClass import *

events = dict()

parser = argparse.ArgumentParser()
parser.add_argument("-d", "--debug",
                    help="enable debug host during script processing",
                    action="store_true",
                    dest="debug")
parser.add_argument("-l", "--debug-ip",
                    help="debuger listen ip address",
                    action="store",
                    dest="debug_ip",
                    type=ipaddress.ip_address,
                    default="127.0.0.1")
parser.add_argument("-p", "--debug-port",
                    help="debuger listen port",
                    action="store",
                    dest="debug_port",
                    type=int,
                    default="5678")
parser.add_argument("-s", "--symbol",
                    help="symbol to parse and analysis",
                    action="store",
                    dest="symbol",
                    type=str,
                    default="native_queued_spin_lock_slowpath")
parser.add_argument("-e", "--event-type",
                    help="event type to analysis",
                    action="store",
                    dest="event_type",
                    type=str,
                    default="cycles:pp")              

args = parser.parse_args()

def trace_begin():
  if args.debug:
    dbg.listen((str(args.debug_ip),args.debug_port))
    dbg.wait_for_client()
    dbg.breakpoint()
  
  # find_latency = lambda e : print(e.sample["weight"])
  # eview.foreach("cpu/mem-loads,ldlat=30/P", find_latency)
  # eview.foreach("cpu/mem-stores/P", find_latency)
  # eview.foreach("cycles:pp", find_latency)
    
def trace_unhandled(event_name, context, event_fields_dict, perf_sample_dict):
  print(get_dict_as_string(event_fields_dict))
  print('Sample: {'+get_dict_as_string(perf_sample_dict['sample'], ', ')+'}')
  
class EventView(object):
  def __init__(self, events):
    self.events = events
    self.ipc_map = dict()

  def get_ipc(self,symbol):
    if symbol == "total":
      return self.get_total_ipc()

  def get_total_ipc(self):
    if "total" not in self.ipc_map:
      self.ipc_map["total"] = self.get_total_instructions() / self.get_total_cycles()

    return self.ipc_map["total"]
      
  def get_total_instructions(self):
    for symbol in ["instructions", "instructions:pp"]:
        insts = self.get_total(symbol)
        if insts != 0:
          return insts
    return 0
  
  def get_total_cycles(self):
    for symbol in ["cycles", "cycles:pp"]:
      cycles = self.get_total(symbol)
      if cycles != 0:
        return cycles
    return 0
    
  def get_total(self, symbol):
    if symbol in self.events:
      return self.events[symbol]["total"]
    return 0
  
  def print_summary(self):
    for key in self.events:
      print("%-20s %8u" % (key, self.events[key]["total"]))

  def foreach(self, symbol, doit):
    if symbol not in self.events:
      return
    for event in self.events[symbol]["el"]:
      doit(event)


def print_header(event_name, cpu, secs, nsecs, pid, comm):
  print("%-20s %5u %05u.%09u %8u %-20s " % \
  (event_name, cpu, secs, nsecs, pid, comm), end="")

def get_dict_as_string(a_dict, delimiter=' '):
  return delimiter.join(['%s=%s'%(k,str(v))for k,v in sorted(a_dict.items())])

def create_event_with_more_info(param_dict):
  event_attr = param_dict["attr"]
  sample     = param_dict["sample"]
  raw_buf    = param_dict["raw_buf"]
  comm       = param_dict["comm"]
  name       = param_dict["ev_name"]
  callchain  = param_dict["callchain"]

  # Symbol and dso info are not always resolved
  if ("dso" in param_dict):
    dso = param_dict["dso"]
  else:
    dso = "Unknown_dso"

  if ("symbol" in param_dict):
    symbol = param_dict["symbol"]
  else:
    symbol = "Unknown_symbol"

  # Create the event object and insert it to the right table in database
  event = create_event(name, comm, dso, symbol, event_attr)
  event.sample = sample
  event.cycles = event.sample["period"]
  event.attr = event_attr
  event.raw_buf = raw_buf
  event.callchain = callchain
  return event


class CallGraphNode(object):
  def __init__(self, symbol: str, cycles: int, level: int):
    self.symbol: str = symbol
    self.cycles: int = cycles
    self.level: int = level
    self.callers: List[CallGraphNode] = []

  def __str__(self):
    if len(self.callers) > 0:
      caller_str = "  " * self.level + str(self.callers)[1:-1]
    else:
      caller_str = ""
    return self.symbol + f": {self.cycles}" + "\n" + caller_str

  def __repr__(self):
    return str(self) 

  def find_caller(self, symbol: str) -> 'CallGraphNode':
    return next((node for node in self.callers if node.symbol == symbol), None)
  
  def add_caller(self, symbol: str, cycles: int) -> 'CallGraphNode':
    caller = self.find_caller(symbol)
    if caller is None:
      caller = CallGraphNode(symbol, cycles, self.level +1)
      self.callers.append(caller)
    else:
      caller.cycles += cycles
    
    return caller

class CallGraph(object):
  def __init__(self, symbol: str):
    self.symbol: str = symbol
    self.root: CallGraphNode = None
  
  def __str__(self):
    return f"Symbol: {self.symbol}\n"+ str(self.root)

  def process_event(self, event):
    if self.root is None:
      self.root = CallGraphNode(event.symbol, event.cycles, 0)
    else:
      self.root.cycles += event.cycles
    # add callchain
    node: CallGraphNode = self.root
    for item in event.callchain[1:]:
      symbol = hex(item['ip'])
      if item['sym'] is not None:
        symbol = item['sym']['name']
      node = node.add_caller(symbol, event.cycles)

graph: CallGraph = None
def create_callgraph_for_function(event, symbol: str):
  global graph 
  if graph is None:
    graph = CallGraph(symbol)
  if event.symbol == symbol:
    graph.process_event(event)

  
  
def process_event(param_dict):
  global events
    
  event = create_event_with_more_info(param_dict)
  if event.symbol == args.symbol and event.name == args.event_type:
    graph = create_callgraph_for_function(event, args.symbol)
  if event.name not in events:
    events[event.name] = {"total":event.sample["period"], "el": [event]}
  else:
    events[event.name]["total"] += event.sample["period"]
    events[event.name]["el"].append(event)

def trace_end():
  eview = EventView(events)
  eview.print_summary()
  print(graph)