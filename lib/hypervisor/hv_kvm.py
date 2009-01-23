#
#

# Copyright (C) 2008 Google Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.


"""KVM hypervisor

"""

import os
import os.path
import re
import tempfile
import time
import logging
from cStringIO import StringIO

from ganeti import utils
from ganeti import constants
from ganeti import errors
from ganeti import serializer
from ganeti import objects
from ganeti.hypervisor import hv_base


class KVMHypervisor(hv_base.BaseHypervisor):
  """KVM hypervisor interface"""

  _ROOT_DIR = constants.RUN_GANETI_DIR + "/kvm-hypervisor"
  _PIDS_DIR = _ROOT_DIR + "/pid" # contains live instances pids
  _CTRL_DIR = _ROOT_DIR + "/ctrl" # contains instances control sockets
  _CONF_DIR = _ROOT_DIR + "/conf" # contains instances startup data
  _DIRS = [_ROOT_DIR, _PIDS_DIR, _CTRL_DIR, _CONF_DIR]

  PARAMETERS = [
    constants.HV_KERNEL_PATH,
    constants.HV_INITRD_PATH,
    constants.HV_ACPI,
    ]

  _MIGRATION_STATUS_RE = re.compile('Migration\s+status:\s+(\w+)',
                                    re.M | re.I)

  def __init__(self):
    hv_base.BaseHypervisor.__init__(self)
    # Let's make sure the directories we need exist, even if the RUN_DIR lives
    # in a tmpfs filesystem or has been otherwise wiped out.
    for mydir in self._DIRS:
      if not os.path.exists(mydir):
        os.mkdir(mydir)

  def _InstancePidAlive(self, instance_name):
    """Returns the instance pid and pidfile

    """
    pidfile = "%s/%s" % (self._PIDS_DIR, instance_name)
    pid = utils.ReadPidFile(pidfile)
    alive = utils.IsProcessAlive(pid)

    return (pidfile, pid, alive)

  def _InstanceMonitor(self, instance_name):
    """Returns the instance monitor socket name

    """
    return '%s/%s.monitor' % (self._CTRL_DIR, instance_name)

  def _InstanceSerial(self, instance_name):
    """Returns the instance serial socket name

    """
    return '%s/%s.serial' % (self._CTRL_DIR, instance_name)

  def _InstanceKVMRuntime(self, instance_name):
    """Returns the instance KVM runtime filename

    """
    return '%s/%s.runtime' % (self._CONF_DIR, instance_name)

  def _WriteNetScript(self, instance, seq, nic):
    """Write a script to connect a net interface to the proper bridge.

    This can be used by any qemu-type hypervisor.

    @param instance: instance we're acting on
    @type instance: instance object
    @param seq: nic sequence number
    @type seq: int
    @param nic: nic we're acting on
    @type nic: nic object
    @return: netscript file name
    @rtype: string

    """
    script = StringIO()
    script.write("#!/bin/sh\n")
    script.write("# this is autogenerated by Ganeti, please do not edit\n#\n")
    script.write("export INSTANCE=%s\n" % instance.name)
    script.write("export MAC=%s\n" % nic.mac)
    script.write("export IP=%s\n" % nic.ip)
    script.write("export BRIDGE=%s\n" % nic.bridge)
    script.write("export INTERFACE=$1\n")
    # TODO: make this configurable at ./configure time
    script.write("if [ -x /etc/ganeti/kvm-vif-bridge ]; then\n")
    script.write("  # Execute the user-specific vif file\n")
    script.write("  /etc/ganeti/kvm-vif-bridge\n")
    script.write("else\n")
    script.write("  # Connect the interface to the bridge\n")
    script.write("  /sbin/ifconfig $INTERFACE 0.0.0.0 up\n")
    script.write("  /usr/sbin/brctl addif $BRIDGE $INTERFACE\n")
    script.write("fi\n\n")
    # As much as we'd like to put this in our _ROOT_DIR, that will happen to be
    # mounted noexec sometimes, so we'll have to find another place.
    (tmpfd, tmpfile_name) = tempfile.mkstemp()
    tmpfile = os.fdopen(tmpfd, 'w')
    tmpfile.write(script.getvalue())
    tmpfile.close()
    os.chmod(tmpfile_name, 0755)
    return tmpfile_name

  def ListInstances(self):
    """Get the list of running instances.

    We can do this by listing our live instances directory and
    checking whether the associated kvm process is still alive.

    """
    result = []
    for name in os.listdir(self._PIDS_DIR):
      filename = "%s/%s" % (self._PIDS_DIR, name)
      if utils.IsProcessAlive(utils.ReadPidFile(filename)):
        result.append(name)
    return result

  def GetInstanceInfo(self, instance_name):
    """Get instance properties.

    @param instance_name: the instance name

    @return: tuple (name, id, memory, vcpus, stat, times)

    """
    pidfile, pid, alive = self._InstancePidAlive(instance_name)
    if not alive:
      return None

    cmdline_file = "/proc/%s/cmdline" % pid
    try:
      fh = open(cmdline_file, 'r')
      try:
        cmdline = fh.read()
      finally:
        fh.close()
    except EnvironmentError, err:
      raise errors.HypervisorError("Failed to list instance %s: %s" %
                                   (instance_name, err))

    memory = 0
    vcpus = 0
    stat = "---b-"
    times = "0"

    arg_list = cmdline.split('\x00')
    while arg_list:
      arg =  arg_list.pop(0)
      if arg == '-m':
        memory = arg_list.pop(0)
      elif arg == '-smp':
        vcpus = arg_list.pop(0)

    return (instance_name, pid, memory, vcpus, stat, times)

  def GetAllInstancesInfo(self):
    """Get properties of all instances.

    @return: list of tuples (name, id, memory, vcpus, stat, times)

    """
    data = []
    for name in os.listdir(self._PIDS_DIR):
      filename = "%s/%s" % (self._PIDS_DIR, name)
      if utils.IsProcessAlive(utils.ReadPidFile(filename)):
        data.append(self.GetInstanceInfo(name))

    return data

  def _GenerateKVMRuntime(self, instance, block_devices, extra_args):
    """Generate KVM information to start an instance.

    """
    pidfile, pid, alive = self._InstancePidAlive(instance.name)
    kvm = constants.KVM_PATH
    kvm_cmd = [kvm]
    kvm_cmd.extend(['-m', instance.beparams[constants.BE_MEMORY]])
    kvm_cmd.extend(['-smp', instance.beparams[constants.BE_VCPUS]])
    kvm_cmd.extend(['-pidfile', pidfile])
    # used just by the vnc server, if enabled
    kvm_cmd.extend(['-name', instance.name])
    kvm_cmd.extend(['-daemonize'])
    if not instance.hvparams[constants.HV_ACPI]:
      kvm_cmd.extend(['-no-acpi'])

    boot_drive = True
    for cfdev, dev_path in block_devices:
      # TODO: handle FD_LOOP and FD_BLKTAP (?)
      if boot_drive:
        boot_val = ',boot=on'
        boot_drive = False
      else:
        boot_val = ''

      # TODO: handle different if= types
      if_val = ',if=virtio'

      drive_val = 'file=%s,format=raw%s%s' % (dev_path, if_val, boot_val)
      kvm_cmd.extend(['-drive', drive_val])

    kvm_cmd.extend(['-kernel', instance.hvparams[constants.HV_KERNEL_PATH]])

    initrd_path = instance.hvparams[constants.HV_INITRD_PATH]
    if initrd_path:
      kvm_cmd.extend(['-initrd', initrd_path])

    kvm_cmd.extend(['-append', 'console=ttyS0,38400 root=/dev/vda'])

    #"hvm_boot_order",
    #"hvm_cdrom_image_path",

    kvm_cmd.extend(['-nographic'])
    # FIXME: handle vnc, if needed
    # How do we decide whether to have it or not?? :(
    #"vnc_bind_address",
    #"network_port"
    monitor_dev = 'unix:%s,server,nowait' % \
      self._InstanceMonitor(instance.name)
    kvm_cmd.extend(['-monitor', monitor_dev])
    serial_dev = 'unix:%s,server,nowait' % self._InstanceSerial(instance.name)
    kvm_cmd.extend(['-serial', serial_dev])

    # Save the current instance nics, but defer their expansion as parameters,
    # as we'll need to generate executable temp files for them.
    kvm_nics = instance.nics

    return (kvm_cmd, kvm_nics)

  def _WriteKVMRuntime(self, instance_name, data):
    """Write an instance's KVM runtime

    """
    try:
      utils.WriteFile(self._InstanceKVMRuntime(instance_name),
                      data=data)
    except EnvironmentError, err:
      raise errors.HypervisorError("Failed to save KVM runtime file: %s" % err)

  def _ReadKVMRuntime(self, instance_name):
    """Read an instance's KVM runtime

    """
    try:
      file_content = utils.ReadFile(self._InstanceKVMRuntime(instance_name))
    except EnvironmentError, err:
      raise errors.HypervisorError("Failed to load KVM runtime file: %s" % err)
    return file_content

  def _SaveKVMRuntime(self, instance, kvm_runtime):
    """Save an instance's KVM runtime

    """
    kvm_cmd, kvm_nics = kvm_runtime
    serialized_nics = [nic.ToDict() for nic in kvm_nics]
    serialized_form = serializer.Dump((kvm_cmd, serialized_nics))
    self._WriteKVMRuntime(instance.name, serialized_form)

  def _LoadKVMRuntime(self, instance, serialized_runtime=None):
    """Load an instance's KVM runtime

    """
    if not serialized_runtime:
      serialized_runtime = self._ReadKVMRuntime(instance.name)
    loaded_runtime = serializer.Load(serialized_runtime)
    kvm_cmd, serialized_nics = loaded_runtime
    kvm_nics = [objects.NIC.FromDict(snic) for snic in serialized_nics]
    return (kvm_cmd, kvm_nics)

  def _ExecuteKVMRuntime(self, instance, kvm_runtime, incoming=None):
    """Execute a KVM cmd, after completing it with some last minute data

    @type incoming: tuple of strings
    @param incoming: (target_host_ip, port)

    """
    pidfile, pid, alive = self._InstancePidAlive(instance.name)
    if alive:
      raise errors.HypervisorError("Failed to start instance %s: %s" %
                                   (instance.name, "already running"))

    temp_files = []

    kvm_cmd, kvm_nics = kvm_runtime

    if not kvm_nics:
      kvm_cmd.extend(['-net', 'none'])
    else:
      for nic_seq, nic in enumerate(kvm_nics):
        nic_val = "nic,macaddr=%s,model=virtio" % nic.mac
        script = self._WriteNetScript(instance, nic_seq, nic)
        kvm_cmd.extend(['-net', nic_val])
        kvm_cmd.extend(['-net', 'tap,script=%s' % script])
        temp_files.append(script)

    if incoming:
      target, port = incoming
      kvm_cmd.extend(['-incoming', 'tcp:%s:%s' % (target, port)])

    result = utils.RunCmd(kvm_cmd)
    if result.failed:
      raise errors.HypervisorError("Failed to start instance %s: %s (%s)" %
                                   (instance.name, result.fail_reason,
                                    result.output))

    if not utils.IsProcessAlive(utils.ReadPidFile(pidfile)):
      raise errors.HypervisorError("Failed to start instance %s: %s" %
                                   (instance.name))

    for filename in temp_files:
      utils.RemoveFile(filename)

  def StartInstance(self, instance, block_devices, extra_args):
    """Start an instance.

    """
    pidfile, pid, alive = self._InstancePidAlive(instance.name)
    if alive:
      raise errors.HypervisorError("Failed to start instance %s: %s" %
                                   (instance.name, "already running"))

    kvm_runtime = self._GenerateKVMRuntime(instance, block_devices, extra_args)
    self._SaveKVMRuntime(instance, kvm_runtime)
    self._ExecuteKVMRuntime(instance, kvm_runtime)

  def _CallMonitorCommand(self, instance_name, command):
    """Invoke a command on the instance monitor.

    """
    socat = ("echo %s | %s STDIO UNIX-CONNECT:%s" %
             (utils.ShellQuote(command),
              constants.SOCAT_PATH,
              utils.ShellQuote(self._InstanceMonitor(instance_name))))
    result = utils.RunCmd(socat)
    if result.failed:
      msg = ("Failed to send command '%s' to instance %s."
             " output: %s, error: %s, fail_reason: %s" %
             (instance.name, result.stdout, result.stderr, result.fail_reason))
      raise errors.HypervisorError(msg)

    return result

  def _RetryInstancePowerdown(self, instance, pid, timeout=30):
    """Wait for an instance  to power down.

    """
    # Wait up to $timeout seconds
    end = time.time() + timeout
    wait = 1
    while time.time() < end and utils.IsProcessAlive(pid):
      self._CallMonitorCommand(instance.name, 'system_powerdown')
      time.sleep(wait)
      # Make wait time longer for next try
      if wait < 5:
        wait *= 1.3

  def StopInstance(self, instance, force=False):
    """Stop an instance.

    """
    pidfile, pid, alive = self._InstancePidAlive(instance.name)
    if pid > 0 and alive:
      if force or not instance.hvparams[constants.HV_ACPI]:
        utils.KillProcess(pid)
      else:
        self._RetryInstancePowerdown(instance, pid)

    if not utils.IsProcessAlive(pid):
      utils.RemoveFile(pidfile)
      utils.RemoveFile(self._InstanceMonitor(instance.name))
      utils.RemoveFile(self._InstanceSerial(instance.name))
      utils.RemoveFile(self._InstanceKVMRuntime(instance.name))
      return True
    else:
      return False

  def RebootInstance(self, instance):
    """Reboot an instance.

    """
    # For some reason if we do a 'send-key ctrl-alt-delete' to the control
    # socket the instance will stop, but now power up again. So we'll resort
    # to shutdown and restart.
    pidfile, pid, alive = self._InstancePidAlive(instance.name)
    if not alive:
      raise errors.HypervisorError("Failed to reboot instance %s: not running" %
                                             (instance.name))
    # StopInstance will delete the saved KVM runtime so:
    # ...first load it...
    kvm_runtime = self._LoadKVMRuntime(instance)
    # ...now we can safely call StopInstance...
    if not self.StopInstance(instance):
      self.StopInstance(instance, force=True)
    # ...and finally we can save it again, and execute it...
    self._SaveKVMRuntime(instance, kvm_runtime)
    self._ExecuteKVMRuntime(instance, kvm_runtime)

  def MigrationInfo(self, instance):
    """Get instance information to perform a migration.

    @type instance: L{objects.Instance}
    @param instance: instance to be migrated
    @rtype: string
    @return: content of the KVM runtime file

    """
    return self._ReadKVMRuntime(instance.name)

  def AcceptInstance(self, instance, info, target):
    """Prepare to accept an instance.

    @type instance: L{objects.Instance}
    @param instance: instance to be accepted
    @type info: string
    @param info: content of the KVM runtime file on the source node
    @type target: string
    @param target: target host (usually ip), on this node

    """
    kvm_runtime = self._LoadKVMRuntime(instance, serialized_runtime=info)
    incoming_address = (target, constants.KVM_MIGRATION_PORT)
    self._ExecuteKVMRuntime(instance, kvm_runtime, incoming=incoming_address)

  def FinalizeMigration(self, instance, info, success):
    """Finalize an instance migration.

    Stop the incoming mode KVM.

    @type instance: L{objects.Instance}
    @param instance: instance whose migration is being aborted

    """
    if success:
      self._WriteKVMRuntime(instance.name, info)
    else:
      self.StopInstance(instance, force=True)

  def MigrateInstance(self, instance_name, target, live):
    """Migrate an instance to a target node.

    The migration will not be attempted if the instance is not
    currently running.

    @type instance_name: string
    @param instance_name: name of the instance to be migrated
    @type target: string
    @param target: ip address of the target node
    @type live: boolean
    @param live: perform a live migration

    """
    pidfile, pid, alive = self._InstancePidAlive(instance_name)
    if not alive:
      raise errors.HypervisorError("Instance not running, cannot migrate")

    if not live:
      self._CallMonitorCommand(instance_name, 'stop')

    migrate_command = ('migrate -d tcp:%s:%s' %
                       (target, constants.KVM_MIGRATION_PORT))
    self._CallMonitorCommand(instance_name, migrate_command)

    info_command = 'info migrate'
    done = False
    while not done:
      result = self._CallMonitorCommand(instance_name, info_command)
      match = self._MIGRATION_STATUS_RE.search(result.stdout)
      if not match:
        raise errors.HypervisorError("Unknown 'info migrate' result: %s" %
                                     result.stdout)
      else:
        status = match.group(1)
        if status == 'completed':
          done = True
        elif status == 'active':
          time.sleep(2)
        elif status == 'failed' or status == 'cancelled':
          if not live:
            self._CallMonitorCommand(instance_name, 'cont')
          raise errors.HypervisorError("Migration %s at the kvm level" %
                                       status)
        else:
          logging.info("KVM: unknown migration status '%s'" % status)
          time.sleep(2)

    utils.KillProcess(pid)
    utils.RemoveFile(pidfile)
    utils.RemoveFile(self._InstanceMonitor(instance_name))
    utils.RemoveFile(self._InstanceSerial(instance_name))
    utils.RemoveFile(self._InstanceKVMRuntime(instance_name))

  def GetNodeInfo(self):
    """Return information about the node.

    @return: a dict with the following keys (values in MiB):
          - memory_total: the total memory size on the node
          - memory_free: the available memory on the node for instances
          - memory_dom0: the memory used by the node itself, if available

    """
    # global ram usage from the xm info command
    # memory                 : 3583
    # free_memory            : 747
    # note: in xen 3, memory has changed to total_memory
    try:
      fh = file("/proc/meminfo")
      try:
        data = fh.readlines()
      finally:
        fh.close()
    except EnvironmentError, err:
      raise errors.HypervisorError("Failed to list node info: %s" % err)

    result = {}
    sum_free = 0
    for line in data:
      splitfields = line.split(":", 1)

      if len(splitfields) > 1:
        key = splitfields[0].strip()
        val = splitfields[1].strip()
        if key == 'MemTotal':
          result['memory_total'] = int(val.split()[0])/1024
        elif key in ('MemFree', 'Buffers', 'Cached'):
          sum_free += int(val.split()[0])/1024
        elif key == 'Active':
          result['memory_dom0'] = int(val.split()[0])/1024
    result['memory_free'] = sum_free

    cpu_total = 0
    try:
      fh = open("/proc/cpuinfo")
      try:
        cpu_total = len(re.findall("(?m)^processor\s*:\s*[0-9]+\s*$",
                                   fh.read()))
      finally:
        fh.close()
    except EnvironmentError, err:
      raise errors.HypervisorError("Failed to list node info: %s" % err)
    result['cpu_total'] = cpu_total

    return result

  @staticmethod
  def GetShellCommandForConsole(instance):
    """Return a command for connecting to the console of an instance.

    """
    # TODO: we can either try the serial socket or suggest vnc
    return "echo Console not available for the kvm hypervisor yet"

  def Verify(self):
    """Verify the hypervisor.

    Check that the binary exists.

    """
    if not os.path.exists(constants.KVM_PATH):
      return "The kvm binary ('%s') does not exist." % constants.KVM_PATH
    if not os.path.exists(constants.SOCAT_PATH):
      return "The socat binary ('%s') does not exist." % constants.SOCAT_PATH


  @classmethod
  def CheckParameterSyntax(cls, hvparams):
    """Check the given parameters for validity.

    For the KVM hypervisor, this only check the existence of the
    kernel.

    @type hvparams:  dict
    @param hvparams: dictionary with parameter names/value
    @raise errors.HypervisorError: when a parameter is not valid

    """
    super(KVMHypervisor, cls).CheckParameterSyntax(hvparams)

    if not hvparams[constants.HV_KERNEL_PATH]:
      raise errors.HypervisorError("Need a kernel for the instance")

    if not os.path.isabs(hvparams[constants.HV_KERNEL_PATH]):
      raise errors.HypervisorError("The kernel path must be an absolute path")

    if hvparams[constants.HV_INITRD_PATH]:
      if not os.path.isabs(hvparams[constants.HV_INITRD_PATH]):
        raise errors.HypervisorError("The initrd path must be an absolute path"
                                     ", if defined")

  def ValidateParameters(self, hvparams):
    """Check the given parameters for validity.

    For the KVM hypervisor, this checks the existence of the
    kernel.

    """
    super(KVMHypervisor, self).ValidateParameters(hvparams)

    kernel_path = hvparams[constants.HV_KERNEL_PATH]
    if not os.path.isfile(kernel_path):
      raise errors.HypervisorError("Instance kernel '%s' not found or"
                                   " not a file" % kernel_path)
    initrd_path = hvparams[constants.HV_INITRD_PATH]
    if initrd_path and not os.path.isfile(initrd_path):
      raise errors.HypervisorError("Instance initrd '%s' not found or"
                                   " not a file" % initrd_path)
