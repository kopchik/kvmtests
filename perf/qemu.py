#!/usr/bin/env python3

from libvmc import KVM, Bridged, Drive, main, \
  __version__ as vmc_version
assert vmc_version >= 12, "vmc library is too old"
from .perftool import get_events
from .utils import retry
from subprocess import check_call
from ipaddress import ip_address
import rpyc

PERF_CMD = "sudo perf kvm stat -e {events} -x, -p {pid} -o {output} sleep {t}"


class Template(KVM):
  cmd    = "qemu-system-x86_64 -enable-kvm -curses"
  name   = "template"
  mem    = 1024
  rpc    = None  # to be filled where rpc.connect is called
  net    = [Bridged(ifname="template", model='e1000',
         mac="52:54:91:5E:38:BB", br="intbr")]
  drives = [Drive("/home/sources/perfvms/template.qcow2",
            cache="unsafe")]
  auto   = True
  rpc    = None
  Popen  = None

  def Popen(self, *args, **kwargs):
    if not self.rpc:
      self.rpc = retry(rpyc.connect, args=(str(self.addr),), \
                        kwargs={"port":6666}, retries=10)
    return self.rpc.root.Popen(*args, **kwargs)

  def stat(self, output):
    from config import MEASURE_TIME, events
    cmd = PERF_CMD.format(events=events, pid=self.pid,
                          output=output, t=MEASURE_TIME)
    check_call(cmd)



if __name__ == '__main__':
  template = Template()
  main()
