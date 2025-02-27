#!/usr/bin/env python3
# --------------------------------------------------------------------------
# This scripts implements the btnap-service.
#
# The script is a modified version of the script found in
# the post by Mike Kazantsev. It adds automatic
# bluetooth authentication (source Merlin Schumacher from ct-magazine).
# See CREDITS for details.
#
# Author: Bernhard Bablok
# License: GPL3
#
# Website: https://github.com/bablokb/pi-btnap
#
# --------------------------------------------------------------------------



import os, sys, time, types, subprocess, signal
import dbus

iface_base = 'org.bluez'
iface_dev = '{}.Device1'.format(iface_base)
iface_adapter = '{}.Adapter1'.format(iface_base)
iface_props = 'org.freedesktop.DBus.Properties'

class BTError(Exception): pass

def get_bus():
	bus = getattr(get_bus, 'cached_obj', None)
	if not bus: bus = get_bus.cached_obj = dbus.SystemBus()
	return bus

def get_manager():
	manager = getattr(get_manager, 'cached_obj', None)
	if not manager:
		manager = get_manager.cached_obj = dbus.Interface(
			get_bus().get_object(iface_base, '/'),
			'org.freedesktop.DBus.ObjectManager' )
	return manager

def prop_get(obj, k, iface=None):
	if iface is None: iface = obj.dbus_interface
	return obj.Get(iface, k, dbus_interface=iface_props)
def prop_set(obj, k, v, iface=None):
	if iface is None: iface = obj.dbus_interface
	return obj.Set(iface, k, v, dbus_interface=iface_props)

def find_adapter(pattern=None):
	return find_adapter_in_objects(get_manager().GetManagedObjects(), pattern)

def find_adapter_in_objects(objects, pattern=None):
	bus, obj = get_bus(), None
	for path, ifaces in objects.items():
		adapter = ifaces.get(iface_adapter)
		if adapter is None: continue
		if not pattern or pattern == adapter['Address'] or path.endswith(pattern):
			obj = bus.get_object(iface_base, path)
			yield dbus.Interface(obj, iface_adapter)
	if obj is None:
		raise BTError('Bluetooth adapter not found')

def find_device(device_address, adapter_pattern=None):
	return find_device_in_objects(get_manager().GetManagedObjects(), device_address, adapter_pattern)

def find_device_in_objects(objects, device_address, adapter_pattern=None):
	bus = get_bus()
	path_prefix = ''
	if adapter_pattern:
		if not isinstance(adapter_pattern, (str,)): adapter = adapter_pattern
		else: adapter = find_adapter_in_objects(objects, adapter_pattern)
		path_prefix = adapter.object_path
	for path, ifaces in objects.items():
		device = ifaces.get(iface_dev)
		if device is None: continue
		if device['Address'] == device_address and path.startswith(path_prefix):
			obj = bus.get_object(iface_base, path)
			return dbus.Interface(obj, iface_dev)
	raise BTError('Bluetooth device not found')


### bt-pan

def main(args=None):
	import argparse
	parser = argparse.ArgumentParser(
		description='BlueZ bluetooth PAN network server/client.')

	parser.add_argument('-i', '--device', metavar='local-addr/pattern',
		help='Local device address/pattern to use (if not default).')
	parser.add_argument('-a', '--device-all', action='store_true',
		help='Use all local hci devices, not just default one.'
			' Only valid with "server" mode, mutually exclusive with --device option.')
	parser.add_argument('-u', '--uuid',
		metavar='uuid_or_shortcut', default='nap',
		help='Service UUID to use. Can be either full UUID'
			' or one of the shortcuts: gn, panu, nap. Default: %(default)s.')
	parser.add_argument('--systemd', action='store_true',
		help='Use systemd service'
			' notification/watchdog mechanisms in daemon modes, if available.')
	parser.add_argument('--debug',
		action='store_true', help='Verbose operation mode.')

	cmds = parser.add_subparsers( dest='call',
		title='Supported operations (have their own suboptions as well)' )

	cmd = cmds.add_parser('server', help='Run infinitely as a NAP network server.')
	cmd.add_argument('iface_name',
		help='Bridge interface name to which each link will be added by bluez.'
			' It must be created and configured before starting the server.')

	cmd = cmds.add_parser('client', help='Connect to a PAN network.')
	cmd.add_argument('remote_addr', help='Remote device address to connect to.')
	cmd.add_argument('-w', '--wait', action='store_true',
		help='Go into an endless wait-loop after connection, terminating it on exit.')
	cmd.add_argument('-c', '--if-not-connected', action='store_true',
		help='Dont raise error if connection is already established.')
	cmd.add_argument('-r', '--reconnect', action='store_true',
		help='Force reconnection if some connection is already established.')

	opts = parser.parse_args()

	global log
	import logging
	logging.basicConfig(level=logging.DEBUG if opts.debug else logging.WARNING)
	log = logging.getLogger()

	if not opts.device_all: devs = [next(iter(find_adapter(opts.device)))]
	else:
		if opts.call != 'server':
			parser.error('--device-all option is only valid with "server" mode.')
		devs = list(find_adapter())
	devs = dict((prop_get(dev, 'Address'), dev) for dev in devs)
	for dev_addr, dev in devs.items():
		prop_set(dev, 'Powered', True)
		log.debug('Using local device (addr: %s): %s', dev_addr, dev.object_path)

	wait_iter_noop = 3600
	if opts.systemd:
		from systemd import daemon
		def wait_iter():
			if not wait_iter.sd_ready:
				daemon.notify('READY=1')
				daemon.notify('STATUS=Running in {} mode...'.format(opts.call))
				wait_iter.sd_ready = True
			time.sleep(wait_iter.timeout)
			if wait_iter.sd_wdt: daemon.notify('WATCHDOG=1')
		wd_pid, wd_usec = (os.environ.get(k) for k in ['WATCHDOG_PID', 'WATCHDOG_USEC'])
		if wd_pid and wd_pid.isdigit() and int(wd_pid) == os.getpid():
			wd_interval = float(wd_usec) / 2e6 # half of interval in seconds
			assert wd_interval > 0, wd_interval
		else: wd_interval = None
		if wd_interval:
			log.debug('Initializing systemd watchdog pinger with interval: %ss', wd_interval)
			wait_iter.sd_wdt, wait_iter.timeout = True, min(wd_interval, wait_iter_noop)
		else: wait_iter.sd_wdt, wait_iter.timeout = False, wait_iter_noop
		wait_iter.sd_ready = False
	else: wait_iter = lambda: time.sleep(wait_iter_noop)
	signal.signal(signal.SIGTERM, lambda sig,frm: sys.exit(0))


	if opts.call == 'server':
		brctl = subprocess.Popen(
			['brctl', 'show', opts.iface_name],
			stdout=open(os.devnull, 'wb'), stderr=subprocess.PIPE )
		brctl_stderr = brctl.stderr.read()
		if brctl.wait() or brctl_stderr:
			p = lambda fmt='',*a,**k: print(fmt.format(*a,**k), file=sys.stderr)
			p('brctl check failed for interface: {}', opts.iface_name)
			p()
			p('Bridge interface must be added and configured before starting server, e.g. with:')
			p('  brctl addbr bnep-bridge')
			p('  brctl setfd bnep-bridge 0')
			p('  brctl stp bnep-bridge off')
			p('  ip addr add 10.101.225.84/24 dev bnep-bridge')
			p('  ip link set bnep-bridge up')
			return 1

		servers = list()
		try:
			for dev_addr, dev in devs.items():
				server = dbus.Interface(dev, 'org.bluez.NetworkServer1')
				server.Unregister(opts.uuid) # in case already registered
				server.Register(opts.uuid, opts.iface_name)
				servers.append(server)
				log.debug( 'Registered uuid %r with'
					' bridge/dev: %s / %s', opts.uuid, opts.iface_name, dev_addr )
			while True: wait_iter()
		except KeyboardInterrupt: pass
		finally:
			if servers:
				for server in servers: server.Unregister(opts.uuid)
				log.debug('Unregistered server uuids')


	elif opts.call == 'client':
		dev_remote = find_device(opts.remote_addr, list(devs.values())[0])
		log.debug( 'Using remote device (addr: %s): %s',
			prop_get(dev_remote, 'Address'), dev_remote.object_path )
		try: dev_remote.ConnectProfile(opts.uuid)
		except: pass # no idea why it fails sometimes, but still creates dbus interface

		net = dbus.Interface(dev_remote, 'org.bluez.Network1')
		for n in range(2):
			try: iface = net.Connect(opts.uuid)
			except dbus.exceptions.DBusException as err:
				if err.get_dbus_name() != 'org.bluez.Error.Failed': raise
				connected = prop_get(net, 'Connected')
				if not connected: raise
				if opts.reconnect:
					log.debug( 'Detected pre-established connection'
						' (iface: %s), reconnecting', prop_get(net, 'Interface') )
					net.Disconnect()
					continue
				if not opts.if_not_connected: raise
			else: break
		log.debug(
			'Connected to network (dev_remote: %s, addr: %s) uuid %r with iface: %s',
			dev_remote.object_path, prop_get(dev_remote, 'Address'), opts.uuid, iface )

		if opts.wait:
			try:
				while True: wait_iter()
			except KeyboardInterrupt: pass
			finally:
				net.Disconnect()
				log.debug('Disconnected from network')


	else: raise ValueError(opts.call)
	log.debug('Finished')

if __name__ == '__main__': sys.exit(main())