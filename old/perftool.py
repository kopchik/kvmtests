#!/usr/bin/env python3

from collections import OrderedDict
from useful.csv import Reader as CSVReader
from useful.log import Log
from .utils import memoized
from signal import SIGTERM
from pprint import pprint
from subprocess import *
import argparse
import termios
import struct
import fcntl
import shlex
import time
import pty
import os


counters_cmd = """perf list %s --no-pager |  grep -v 'List of' | awk '{print $1}' | grep -v '^$'"""
NOT_SUPPORTED = '<not supported>'
NOT_COUNTED = '<not counted>'
os.environ["PERF_PAGER"]="cat"
log = Log("perftool")
BUF_SIZE = 65535

def osexec(cmd):
  cmd = shlex.split(cmd)
  os.execlp(cmd[0], *cmd)


def _get_events(hw=True, sw=True, cache=True, tp=True):
  selector = ""
  if hw: selector += " hw"
  if sw: selector += " sw"
  if cache: selector += " cache"
  if tp: selector += " tracepoint"

  cmd = counters_cmd % selector
  raw = check_output(cmd, shell=True)
  return raw.decode().strip(' \n').split('\n')



@memoized('/tmp/get_events.pickle')
def get_events():
  """select counters that are the most useful for our purposes"""

  bad = "kvmmmu:kvm_mmu_get_page,kvmmmu:kvm_mmu_sync_page," \
        "kvmmmu:kvm_mmu_unsync_page,kvmmmu:kvm_mmu_prepare_zap_page".split(',')
  result =  _get_events(tp=False)
  result = ['cycles' if x=='cpu-cycles' else x for x in result]  # replace cpu-cycles with cycles
  #tpevents = get_events(tp=True)
  #for prefix in ['kvm:', 'kvmmmu:', 'vmscan:', 'irq:', 'signal:', 'kmem:', 'power:']:
  #  result += filter(lambda ev: ev.startswith(prefix), tpevents)
  #result = filter(lambda x: x not in bad, result)
  #TODO result += ['irq:*', 'signal:*', 'kmem:*']

  # filter out events that are not supported
  p = Popen(shlex.split('bzip2 -k /dev/urandom -c'), stdout=DEVNULL)
  perf_data = stat(p.pid, result, t=0.5, ann="test run")
  p.send_signal(SIGTERM)
  clean = []
  for k, v in perf_data.items():
    if v is False:
      log.notice("event %s not supported"%k)
      #result.remove(k)
      continue
    elif v is None:
      log.critical("event %s not counted"%k)
      #result.remove(k)
      continue
    clean += [k]
  return clean


def oldstat(pid, events, t, ann=None, norm=False, guest=False):
  evcmd = ",".join(events)
  if guest:
    cmd = "sudo perf kvm stat"
  else:
    cmd = "sudo perf stat"
  cmd += " -e {events} --log-fd 1 -x, -p {pid}".format(events=evcmd, pid=pid)
  pid, fd = pty.fork()
  if pid == 0:
    osexec(cmd)
  # fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("hhhh", 24, 80, 0, 0)) # resise terminal

  # disable echo
  flags = termios.tcgetattr(fd)
  flags[3] &= ~termios.ECHO
  termios.tcsetattr(fd, termios.TCSADRAIN, flags)

  time.sleep(t)
  ctrl_c = termios.tcgetattr(fd)[-1][termios.VINTR]  # get Ctrl+C character
  os.write(fd, ctrl_c)
  os.waitpid(pid, 0)
  raw = b""
  while True:
    try:
      chunk = os.read(fd, BUF_SIZE)
    except OSError:
      break
    if not chunk:
      break
    raw += chunk
  return PerfData(raw, ann=ann, norm=norm)


def stat(pid=None, events=[], time=0, perf="perf", guest=False, extra=""):
  # parse input
  assert events, and time
  CMD = "{perf} kvm" if guest else "{perf}"
  CMD += " stat -e {events} --log-fd {fd} -x, {extra} sleep {time}"
  if pid: extra += " -p {pid}"
  # prepare cmd and call it
  read, write = socketpair()
  cmd = CMD.format(perf=perf, pid=pid, events=",".join(events), \
                   fd=write.fileno(), time=time, extra=extra)
  check_call(shlex.split(cmd), pass_fds=[write.fileno()])  # TODO: buf overflow??
  result = read.recv(100000).decode()
  # parse output of perf
  r = {}
  for s in result.splitlines():
    rawcnt,_,ev = s.split(',')
    if rawcnt == '<not counted>':
      raise NotCountedError
    r[ev] = int(rawcnt)
  return r

def kvmstat(*args, **kwargs):
  return stat(*args, guest=True, **kwargs)

def cgstat(*args, path=None, cpus=None, **kwargs):
  assert path and cpus
  extra = "-C {cpus} -G {path}".format(path=path, cpus=",".join(map(lambda x: str(x), cpus)))
  return stat(*args, extra=extra, **kwargs)


class PerfData(OrderedDict):
  def __init__(self, rawdata, ann=None, norm=False):
    #log.notice("raw data:\n %s" % rawdata.decode())
    # TODO: annotation ann
    super().__init__()

    array = rawdata.decode().split('\r\n')
    # skipping preamble
    if array[0].startswith("#"):
      preamble = array.pop(0)
    # skip empty lines and warnings
    for x in array:
      if not x or x.find('Warning:') != -1:
        array.remove(x)
    assert 'Not all events could be opened.' not in array
    # convert raw values to int or float
    array = map(lambda x: x.split(','), array)
    for entry in array:
      try:
        raw_value, key = entry
        if key.endswith(':G'):  # 'G' stands for guest
          key, _ = key.cplit(':G')
      except ValueError as err:
        log.critical("exception: %s" % err)
        log.critical("entry was: %s" % entry)
        continue
      if raw_value == NOT_SUPPORTED:
        value = False
      elif raw_value == NOT_COUNTED:
        value = None
      else:
        if raw_value.find('.') != -1:
          value = float(raw_value)
        else:
          value = int(raw_value)
      self[key] = value
    # ensure common data layout
    if self.get('cpu-cycles', None):
      self['cycles'] = self['cpu-cycles']
      # del self['cpu-cycles']
    # normalize values if requested
    if norm:
      self.normalize()


  def normalize(self):
    assert 'cycles' in self, "norm=True needs cycles to be measured"
    cycles = self['cycles']
    if abs(cycles-1) <0.01:  # already normalized
      return
    for k, v in self.items():
      if isinstance(v, (int,float)):
          self[k] = v/cycles
    self['normalized'] = True


  def __repr__(self):
    result = []
    for k,v in self.items():
      if isinstance(v, (int,float)):
        result += ["%s=%.5s"%(k,v)]
      else:
        result += ["%s=\"%s\""%(k,v)]
    # result = ["%s=%.5s"%(k,v) for k,v in self.items() if isinstance(v,(int,float))]
    return "Perf(%s)" % (", ".join(result))


def pperf(perf):
  """pretty perf data printer"""
  cycles = perf['cycles']
  for key, value in perf.items():
    if isinstance(value, int):
      print(key, ":", value/cycles)
    else:
      print(key, value)


def pidof(psname, exact=False):
  psname = psname

  pids = (pid for pid in os.listdir('/proc') if pid.isdigit())
  result = []
  for pid in pids:
    name, *cmd = open(os.path.join('/proc', pid, 'cmdline'), 'rt').read().strip('\x00').split('\x00')
    if exact:
      if name == psname:
        result += [pid]
    else:
      if name.startswith(psname):
        result += [pid]
  if not result:
    raise Exception("no such process: %s (exact=%s)" % (psname, exact))
  return result


def f2list(fname):
  """ read samples from file """
  ipcs, ts = [], []
  with open(fname) as csvfile:
    cycles = 0
    for ipc, c in CSVReader(csvfile, type=(float,int)):
      cycles += c
      t = cycles*CYCLE*1000
      if t > 100:  # it is pointless to dig deeper
        break
      ipcs.append(ipc)
      ts.append(t)
    return ts, ipcs

def f2list(fname):
  ts, ins = [], []
  with open(fname) as csvfile:
    t_prev = 0
    for t, i, _ in CSVReader(csvfile, type=(float,int,str), errors='ignore'):
      delta = t - t_prev
      t_prev = t
      ipc = i/delta/FREQ
      #if ipc <0.7: continue
      ts.append(t*1000)
      ins.append(ipc)
    return ts, ins




if __name__ == '__main__':
  parser = argparse.ArgumentParser(description='Run experiments')
  parser.add_argument('-t', '--time', type=float, default=10, help="measurement time")
  parser.add_argument('--debug', default=False, const=True, action='store_const', help='enable debug mode')
  parser.add_argument('-e', '--events', default=False, const=True, action='store_const', help="get useful events")
  group = parser.add_mutually_exclusive_group(required=True)
  group.add_argument('--kvmpid', type=int, help="pid of qemu process")
  group.add_argument('--pid', type=int, help="pid of normal process")
  args = parser.parse_args()
  print(args)

  stat_args = dict(pid=args.pid if args.pid else args.kvmpid, t=args.time, ann="example output", norm=True)
  if args.kvmpid:
    r = kvmstat(events=get_events(), **stat_args)
  elif args.pid:
     r = stat(events=get_events(), **stat_args)
  else:
    r = get_events()
    r = ",".join(r)

  pprint(r)
