# -*- coding: utf-8 -*-
# Copyright (c) 2015, Frappe Technologies and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import os
import frappe
import libzfs
import subprocess
from frappe.utils import cint
from frappe.model.document import Document
from zfs_admin.utils import run_command, sync_properties

class ZFSPool(Document):
	@property
	def zpool(self):
		if not hasattr(self, "_zpool"):
			self._zpool = libzfs.ZFS().get(self.name)
		return self._zpool

	def sync(self):
		self.sync_properties()
		self.sync_vdev()
		self.save()
		self.sync_datasets()

	def on_update(self):
		self.update_disks()

	def sync_vdev(self):
		"""Sync virtual devices"""
		self.load_vdevs()
		self.fix_vdev_ordering()

	def sync_datasets(self):
		"""Sync dataset info"""
		self.added = []

		# sync root dataset
		self.sync_one_dataset(self.zpool.root_dataset)
		self.added.append(self.zpool.root_dataset.name)

		# sync all children
		for c in self.zpool.root_dataset.children_recursive:
			self.sync_one_dataset(c)

		# sync all snapshots
		for c in self.zpool.root_dataset.snapshots_recursive:
			self.sync_one_dataset(c)

		# delete unsued
		for d in frappe.db.sql_list("""select name from `tabZFS Dataset`
			where zfs_pool = %s and name not in ({0})""".format(", ".join(["%s"] * len(self.added))),
				[self.name] + self.added):
			frappe.delete_doc("ZFS Dataset", d)

	def sync_one_dataset(self, d):
		if frappe.db.exists("ZFS Dataset", d.name):
			zdataset = frappe.get_doc("ZFS Dataset", d.name)
		else:
			zdataset = frappe.new_doc("ZFS Dataset")
			zdataset.name = d.name

		zdataset.sync_properties(d)
		zdataset.zfs_pool = self.name
		zdataset.save()

		self.added.append(zdataset.name)

	def update_disks(self):
		"""Update Disk with pool and health status"""
		for vdev in self.virtual_devices:
			if vdev.type == "disk":
				disk_name = vdev.device_name

				# TODO: diskname wit partion suffix like ada0p1
				# this needs to be synced better
				if disk_name[-2] == "p":
					disk_name = disk_name[:-2]

				disk = frappe.get_doc("Disk", disk_name)
				disk.zfs_pool = self.name
				disk.health = vdev.status
				disk.save()

	def load_vdevs(self):
		"""Load videvs from libzfs"""
		for group in ("data", "cache", "log", "spare"):
			group_vdevs = self.zpool.groups.get(group)
			added = {}

			for i, vdev in enumerate(group_vdevs):
				parent_row = self.add_vdev(vdev)

				if parent_row.type == "disk":
					parent_row.device_name = self.get_disk_name(vdev.path)
				else:
					vdev_len = len([v for v in group_vdevs if v.type==vdev.type])
					if vdev_len > 1:
						# name as mirror-1, mirror-2 etc.
						vdev_id = added.setdefault(vdev.type, 1)
						parent_row.device_name = "{0}-{1}".format(vdev.type, vdev_id)
						added[vdev.type] += 1
					else:
						parent_row.device_name = vdev.type

					for disk in vdev.children:
						row = self.add_vdev(disk, True)
						row.group_type = parent_row.type
						row.device_name = self.get_disk_name(disk.path)
						row.parent_device_name = parent_row.device_name

	def fix_vdev_ordering(self):
		"""Remove unused vdev records and order them so that the groups and disks
		appear below each other"""
		new_list = []
		for d in getattr(self, "virtual_devices", []):
			if getattr(d, "mapped", False):
				new_list.append(d)

		# reorder in groups
		new_order = []
		for d in new_list:
			if d.parent_device_name: continue
			new_order.append(d)
			d.idx = len(new_order)
			if d.type != "disk":
				for child in new_list:
					if child.parent_device_name == d.device_name:
						new_order.append(child)
						child.idx = len(new_order)

		self.virtual_devices = new_order

	def get_disk_name(self, disk_path):
		return os.path.split(disk_path)[-1]

	def add_vdev(self, vdev, is_child=False):
		"""Add a new virtual device row"""
		row = self.get_vdev_row(vdev.guid)
		if not row:
			row = self.append("virtual_devices", {"guid": vdev.guid})

		row.status = vdev.status
		row.type = vdev.type
		row.guid = vdev.guid
		if not is_child:
			row.size = vdev.size

		row.mapped = True

		return row

	def sync_properties(self):
		"""Sync ZFS Pool properties"""
		sync_properties(self, self.zpool.properties)

	def get_vdev_row(self, guid):
		for d in getattr(self, "virtual_devices", []):
			if str(d.guid) == str(guid):
				return d

	def zpool_add(self, type, disk1, disk2):
		"""Runs zpool add"""
		self.has_permission("write")

		if type.lower()=="disk":
			args = ["sudo", "zpool", "add", self.name, disk1]
		else:
			args = ["sudo", "zpool", "add", self.name, type.lower(), disk1, disk2]

		out = run_command(args)
		if out=="okay":
			self.sync()
			return out

	def zpool_detach(self, disk):
		"""Runs zpool detach"""
		self.has_permission("write")
		out = run_command(["sudo", "zpool", "detach", self.name, disk])
		if out=="okay":
			self.sync()
			return out

	def zpool_destroy(self):
		"""Runs zpool destroy"""
		self.has_permission("delete")

		out = run_command(["sudo", "zpool", "destroy", self.name])
		if out=="okay":
			# remove references from disk
			frappe.db.sql("update tabDisk set zfs_pool='' where zfs_pool=%s", self.name)

			# delete zfs dataset records
			for d in frappe.db.get_all("ZFS Dataset", filters={"zfs_pool": self.name}):
				frappe.delete_doc("ZFS Dataset", d.name)

			# delete record
			self.delete()
			return "okay"

def zpool_create(name, type, disk1, disk2):
	"""zpool create"""
	if type=="Disk":
		args = ["sudo", "zpool", "create", name, disk1]
	else:
		args = ["sudo", "zpool", "create", name, type.lower(), disk1, disk2]

	if run_command(args)=="okay":
		zfs_pool = frappe.new_doc("ZFS Pool")
		zfs_pool.name = name
		zfs_pool.sync()
		return "okay"
