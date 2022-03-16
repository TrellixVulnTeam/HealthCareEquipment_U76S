# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

"""
In this module resides the core data structures and logic of the plugin system. It is implemented in an OctoPrint-agnostic
way and could be extracted into a separate Python module in the future.

.. autoclass:: PluginManager
   :members:

.. autoclass:: PluginInfo
   :members:

.. autoclass:: Plugin
   :members:

.. autoclass:: RestartNeedingPlugin
   :members:

.. autoclass:: SortablePlugin
   :members:

"""

__license__ = 'GNU Affero General Public License http://www.gnu.org/licenses/agpl.html'
__copyright__ = "Copyright (C) 2014 The OctoPrint Project - Released under terms of the AGPLv3 License"


import os
import io
from collections import defaultdict, namedtuple, OrderedDict
import logging
import fnmatch
import inspect
import sys

import pkg_resources
import pkginfo

from past.builtins import unicode

from octoprint.util import to_unicode, sv
from octoprint.util.version import is_python_compatible, get_python_version_string

try:
	from os import scandir
except ImportError:
	from scandir import scandir


if sys.version_info[0] == 2:
	# noinspection PyDeprecation
	import imp
else:
	# deprecated in Python 3.4+ and hence vendored for now
	import octoprint.vendor.imp as imp


# noinspection PyDeprecation
def _find_module(name, path=None):
	if path is not None:
		spec = imp.find_module(name, [path])
	else:
		spec = imp.find_module(name)
	return spec[1], spec


# noinspection PyDeprecation
def _load_module(name, spec):
	f, filename, details = spec
	return imp.load_module(name, f, filename, details)


EntryPointOrigin = namedtuple("EntryPointOrigin", "type, entry_point, module_name, package_name, package_version")
FolderOrigin = namedtuple("FolderOrigin", "type, folder")
ModuleOrigin = namedtuple("PackageOrigin", "type, module_name, folder")

class PluginInfo(object):
	"""
	The :class:`PluginInfo` class wraps all available information about a registered plugin.

	This includes its meta data (like name, description, version, etc) as well as the actual plugin extensions like
	implementations, hooks and helpers.

	It works on Python module objects and extracts the relevant data from those via accessing the
	:ref:`control properties <sec-plugins-controlproperties>`.

	Arguments:
	    key (str): Identifier of the plugin
	    location (str): Installation folder of the plugin
	    instance (module): Plugin module instance - this may be ``None`` if the plugin has been blacklisted!
	    name (str): Human readable name of the plugin
	    version (str): Version of the plugin
	    description (str): Description of the plugin
	    author (str): Author of the plugin
	    url (str): URL of the website of the plugin
	    license (str): License of the plugin
	"""

	attr_name = '__plugin_name__'
	""" Module attribute from which to retrieve the plugin's human readable name. """

	attr_description = '__plugin_description__'
	""" Module attribute from which to retrieve the plugin's description. """

	attr_disabling_discouraged = '__plugin_disabling_discouraged__'
	""" Module attribute from which to retrieve the reason why disabling the plugin is discouraged. Only effective if ``self.bundled`` is True. """

	attr_version = '__plugin_version__'
	""" Module attribute from which to retrieve the plugin's version. """

	attr_author = '__plugin_author__'
	""" Module attribute from which to retrieve the plugin's author. """

	attr_url = '__plugin_url__'
	""" Module attribute from which to retrieve the plugin's website URL. """

	attr_license = '__plugin_license__'
	""" Module attribute from which to retrieve the plugin's license. """

	attr_pythoncompat = '__plugin_pythoncompat__'
	""" 
	Module attribute from which to retrieve the plugin's python compatibility string. 
	
	If unset a default of ``>=2.7,<3`` will be assumed, meaning that the plugin will be considered compatible to
	Python 2 but not Python 3.
	
	To mark a plugin as Python 3 compatible, a string of ``>=2.7,<4`` is recommended.
	
	Bundled plugins will automatically be assumed to be compatible.
	"""

	attr_hidden = '__plugin_hidden__'
	"""
	Module attribute from which to determine if the plugin's hidden or not.
	
	Only evaluated for bundled plugins, in order to hide them from the Plugin Manager
	and similar places.
	"""

	attr_hooks = '__plugin_hooks__'
	""" Module attribute from which to retrieve the plugin's provided hooks. """

	attr_implementation = '__plugin_implementation__'
	""" Module attribute from which to retrieve the plugin's provided mixin implementation. """

	attr_implementations = '__plugin_implementations__'
	"""
	Module attribute from which to retrieve the plugin's provided implementations.

	This deprecated attribute will only be used if a plugin does not yet offer :attr:`attr_implementation`. Only the
	first entry will be evaluated.

	.. deprecated:: 1.2.0-dev-694

	   Use :attr:`attr_implementation` instead.
	"""

	attr_helpers = '__plugin_helpers__'
	""" Module attribute from which to retrieve the plugin's provided helpers. """

	attr_check = '__plugin_check__'
	""" Module attribute which to call to determine if the plugin can be loaded. """

	attr_init = '__plugin_init__'
	"""
	Module attribute which to call when loading the plugin.

	This deprecated attribute will only be used if a plugin does not yet offer :attr:`attr_load`.

	.. deprecated:: 1.2.0-dev-720

	   Use :attr:`attr_load` instead.
	"""

	attr_load = '__plugin_load__'
	""" Module attribute which to call when loading the plugin. """

	attr_unload = '__plugin_unload__'
	""" Module attribute which to call when unloading the plugin. """

	attr_enable = '__plugin_enable__'
	""" Module attribute which to call when enabling the plugin. """

	attr_disable = '__plugin_disable__'
	""" Module attribute which to call when disabling the plugin. """

	def __init__(self, key, location, instance, name=None, version=None, description=None, author=None, url=None, license=None, parsed_metadata=None):
		self.key = key
		self.location = location
		self.instance = instance
		self.origin = None
		self.enabled = True
		self.blacklisted = False
		self.forced_disabled = False
		self.incompatible = False
		self.bundled = False
		self.loaded = False
		self.managable = True
		self.needs_restart = False
		self.invalid_syntax = False

		self._name = name
		self._version = version
		self._description = description
		self._author = author
		self._url = url
		self._license = license

		self._logger = logging.getLogger(__name__)

		self._cached_parsed_metadata = parsed_metadata
		if self._cached_parsed_metadata is None:
			self._cached_parsed_metadata = self._parse_metadata()

	def validate(self, phase, additional_validators=None):
		result = True

		if phase == "before_import":
			result = not self.forced_disabled and not self.blacklisted and not self.incompatible and not self.invalid_syntax and result

		elif phase == "before_load":
			# if the plugin still uses __plugin_init__, log a deprecation warning and move it to __plugin_load__
			if hasattr(self.instance, self.__class__.attr_init):
				if not hasattr(self.instance, self.__class__.attr_load):
					# deprecation warning
					import warnings
					warnings.warn("{name} uses deprecated control property __plugin_init__, use __plugin_load__ instead".format(name=self.key), DeprecationWarning)

					# move it
					init = getattr(self.instance, self.__class__.attr_init)
					setattr(self.instance, self.__class__.attr_load, init)

				# delete __plugin_init__
				delattr(self.instance, self.__class__.attr_init)

		elif phase == "after_load":
			# if the plugin still uses __plugin_implementations__, log a deprecation warning and put the first
			# item into __plugin_implementation__
			if hasattr(self.instance, self.__class__.attr_implementations):
				if not hasattr(self.instance, self.__class__.attr_implementation):
					# deprecation warning
					import warnings
					warnings.warn("{name} uses deprecated control property __plugin_implementations__, use __plugin_implementation__ instead - only the first implementation of {name} will be recognized".format(name=self.key), DeprecationWarning)

					# put first item into __plugin_implementation__
					implementations = getattr(self.instance, self.__class__.attr_implementations)
					if len(implementations) > 0:
						setattr(self.instance, self.__class__.attr_implementation, implementations[0])

				# delete __plugin_implementations__
				delattr(self.instance, self.__class__.attr_implementations)

		if additional_validators is not None:
			for validator in additional_validators:
				result = validator(phase, self) and result

		return result

	def __str__(self):
		if self.version:
			return "{name} ({version})".format(name=self.name, version=self.version)
		else:
			return to_unicode(self.name)

	def long_str(self, show_bundled=False, bundled_strs=(" [B]", ""),
	             show_location=False, location_str=" - {location}",
	             show_enabled=False, enabled_strs=("* ", "  ", "X ", "C ")):
		"""
		Long string representation of the plugin's information. Will return a string of the format ``<enabled><str(self)><bundled><location>``.

		``enabled``, ``bundled`` and ``location`` will only be displayed if the corresponding flags are set to ``True``.
		The will be filled from ``enabled_str``, ``bundled_str`` and ``location_str`` as follows:

		``enabled_str``
		    a 4-tuple, the first entry being the string to insert when the plugin is enabled, the second
		    entry the string to insert when it is not, the third entry the string when it is blacklisted
		    and the fourth when it is incompatible.
		``bundled_str``
		    a 2-tuple, the first entry being the string to insert when the plugin is bundled, the second
		    entry the string to insert when it is not.
		``location_str``
		    a format string (to be parsed with ``str.format``), the ``{location}`` placeholder will be
		    replaced with the plugin's installation folder on disk.

		Arguments:
		    show_enabled (boolean): whether to show the ``enabled`` part
		    enabled_strs (tuple): the 2-tuple containing the two possible strings to use for displaying the enabled state
		    show_bundled (boolean): whether to show the ``bundled`` part
		    bundled_strs(tuple): the 2-tuple containing the two possible strings to use for displaying the bundled state
		    show_location (boolean): whether to show the ``location`` part
		    location_str (str): the format string to use for displaying the plugin's installation location

		Returns:
		    str: The long string representation of the plugin as described above
		"""
		if show_enabled:
			if self.incompatible:
				ret = to_unicode(enabled_strs[3])
			elif self.blacklisted:
				ret = to_unicode(enabled_strs[2])
			elif not self.enabled:
				ret = to_unicode(enabled_strs[1])
			else:
				ret = to_unicode(enabled_strs[0])
		else:
			ret = ""

		ret += unicode(self)

		if show_bundled:
			ret += to_unicode(bundled_strs[0]) if self.bundled else to_unicode(bundled_strs[1])

		if show_location and self.location:
			ret += to_unicode(location_str).format(location=self.location)

		return ret

	def get_hook(self, hook):
		"""
		Arguments:
		    hook (str): Hook to return.

		Returns:
		    callable or None: Handler for the requested ``hook`` or None if no handler is registered.
		"""

		if not hook in self.hooks:
			return None
		return self.hooks[hook]

	def get_implementation(self, *types):
		"""
		Arguments:
		    types (list): List of :class:`Plugin` sub classes all returned implementations need to implement.

		Returns:
		    object: The plugin's implementation if it matches all of the requested ``types``, None otherwise.
		"""

		if not self.implementation:
			return None

		for t in types:
			if not isinstance(self.implementation, t):
				return None

		return self.implementation

	@property
	def name(self):
		"""
		Human readable name of the plugin. Will be taken from name attribute of the plugin module if available,
		otherwise from the ``name`` supplied during construction with a fallback to ``key``.

		Returns:
		    str: Name of the plugin, fallback is the plugin's identifier.
		"""
		return self._get_instance_attribute(self.__class__.attr_name,
		                                    defaults=(self._name, self.key),
		                                    incl_metadata=True)

	@property
	def description(self):
		"""
		Description of the plugin. Will be taken from the description attribute of the plugin module as defined in
		:attr:`attr_description` if available, otherwise from the ``description`` supplied during construction.
		May be None.

		Returns:
		    str or None: Description of the plugin.
		"""
		return self._get_instance_attribute(self.__class__.attr_description,
		                                    default=self._description,
		                                    incl_metadata=True)

	@property
	def disabling_discouraged(self):
		"""
		Reason why disabling of this plugin is discouraged. Only evaluated for bundled plugins! Will be taken from
		the disabling_discouraged attribute of the plugin module as defined in :attr:`attr_disabling_discouraged` if
		available. False if unset or plugin not bundled.

		Returns:
		    str or None: Reason why disabling this plugin is discouraged (only for bundled plugins)
		"""
		return self._get_instance_attribute(self.__class__.attr_disabling_discouraged, default=False) if self.bundled \
			else False

	@property
	def version(self):
		"""
		Version of the plugin. Will be taken from the version attribute of the plugin module as defined in
		:attr:`attr_version` if available, otherwise from the ``version`` supplied during construction. May be None.

		Returns:
		    str or None: Version of the plugin.
		"""
		return self._version if self._version is not None else self._get_instance_attribute(self.__class__.attr_version,
		                                                                                    default=self._version,
		                                                                                    incl_metadata=True)

	@property
	def author(self):
		"""
		Author of the plugin. Will be taken from the author attribute of the plugin module as defined in
		:attr:`attr_author` if available, otherwise from the ``author`` supplied during construction. May be None.

		Returns:
		    str or None: Author of the plugin.
		"""
		return self._get_instance_attribute(self.__class__.attr_author,
		                                    default=self._author,
		                                    incl_metadata=True)

	@property
	def url(self):
		"""
		Website URL for the plugin. Will be taken from the url attribute of the plugin module as defined in
		:attr:`attr_url` if available, otherwise from the ``url`` supplied during construction. May be None.

		Returns:
		    str or None: Website URL for the plugin.
		"""
		return self._get_instance_attribute(self.__class__.attr_url,
		                                    default=self._url,
		                                    incl_metadata=True)

	@property
	def license(self):
		"""
		License of the plugin. Will be taken from the license attribute of the plugin module as defined in
		:attr:`attr_license` if available, otherwise from the ``license`` supplied during construction. May be None.

		Returns:
		    str or None: License of the plugin.
		"""
		return self._get_instance_attribute(self.__class__.attr_license,
		                                    default=self._license,
		                                    incl_metadata=True)

	@property
	def pythoncompat(self):
		"""
		Python compatibility string of the plugin module as defined in :attr:`attr_pythoncompat` if available, otherwise
		defaults to ``>=2.7,<3``.

		Returns:
		    str: Python compatibility string of the plugin
		"""
		return self._get_instance_attribute(self.__class__.attr_pythoncompat,
		                                    default=">=2.7,<3",
		                                    incl_metadata=True)

	@property
	def hidden(self):
		"""
		Hidden flag.

		Returns:
		    bool: Whether the plugin should be flagged as hidden or not
		"""
		return self._get_instance_attribute(self.__class__.attr_hidden,
		                                    default=False,
		                                    incl_metadata=True)

	@property
	def hooks(self):
		"""
		Hooks provided by the plugin. Will be taken from the hooks attribute of the plugin module as defined in
		:attr:`attr_hooks` if available, otherwise an empty dictionary is returned.

		Returns:
		    dict: Hooks provided by the plugin.
		"""
		return self._get_instance_attribute(self.__class__.attr_hooks, default={})

	@property
	def implementation(self):
		"""
		Implementation provided by the plugin. Will be taken from the implementation attribute of the plugin module
		as defined in :attr:`attr_implementation` if available, otherwise None is returned.

		Returns:
		    object: Implementation provided by the plugin.
		"""
		return self._get_instance_attribute(self.__class__.attr_implementation, default=None)

	@property
	def helpers(self):
		"""
		Helpers provided by the plugin. Will be taken from the helpers attribute of the plugin module as defined in
		:attr:`attr_helpers` if available, otherwise an empty list is returned.

		Returns:
		    dict: Helpers provided by the plugin.
		"""
		return self._get_instance_attribute(self.__class__.attr_helpers, default={})

	@property
	def check(self):
		"""
		Method for pre-load check of plugin. Will be taken from the check attribute of the plugin module as defined in
		:attr:`attr_check` if available, otherwise a lambda always returning True is returned.

		Returns:
		    callable: Check method for the plugin module which should return True if the plugin can be loaded, False
		        otherwise.
		"""
		return self._get_instance_attribute(self.__class__.attr_check, default=lambda: True)

	@property
	def load(self):
		"""
		Method for loading the plugin module. Will be taken from the load attribute of the plugin module as defined
		in :attr:`attr_load` if available, otherwise a no-operation lambda will be returned.

		Returns:
		    callable: Load method for the plugin module.
		"""
		return self._get_instance_attribute(self.__class__.attr_load, default=lambda: True)

	@property
	def unload(self):
		"""
		Method for unloading the plugin module. Will be taken from the unload attribute of the plugin module as defined
		in :attr:`attr_unload` if available, otherwise a no-operation lambda will be returned.

		Returns:
		    callable: Unload method for the plugin module.
		"""
		return self._get_instance_attribute(self.__class__.attr_unload, default=lambda: True)

	@property
	def enable(self):
		"""
		Method for enabling the plugin module. Will be taken from the enable attribute of the plugin module as defined
		in :attr:`attr_enable` if available, otherwise a no-operation lambda will be returned.

		Returns:
		    callable: Enable method for the plugin module.
		"""
		return self._get_instance_attribute(self.__class__.attr_enable, default=lambda: True)

	@property
	def disable(self):
		"""
		Method for disabling the plugin module. Will be taken from the disable attribute of the plugin module as defined
		in :attr:`attr_disable` if available, otherwise a no-operation lambda will be returned.

		Returns:
		    callable: Disable method for the plugin module.
		"""
		return self._get_instance_attribute(self.__class__.attr_disable, default=lambda: True)

	def _get_instance_attribute(self, attr, default=None, defaults=None, incl_metadata=False):
		if self.instance is None or not hasattr(self.instance, attr):
			if incl_metadata and attr in self.parsed_metadata:
				return self.parsed_metadata[attr]

			elif defaults is not None:
				for value in defaults:
					if callable(value):
						value = value()
					if value is not None:
						return value

			return default

		return getattr(self.instance, attr)

	@property
	def parsed_metadata(self):
		return self._cached_parsed_metadata

	def _parse_metadata(self):
		result = dict()

		path = self.location
		if not path:
			return result

		if os.path.isdir(path):
			path = os.path.join(self.location, "__init__.py")

		if not os.path.isfile(path):
			return result

		if not path.endswith(".py"):
			# we only support parsing plain text source files
			return result

		self._logger.debug("Parsing plugin metadata for {} from AST of {}".format(self.key, path))

		try:
			import ast

			with io.open(path, 'rb') as f:
				root = ast.parse(f.read(), filename=path)

			assignments = list(filter(lambda x: isinstance(x, ast.Assign) and x.targets,
			                          root.body))

			def extract_target_ids(node):
				return list(map(lambda x: x.id,
				                filter(lambda x: isinstance(x, ast.Name), node.targets)))

			for key in (self.__class__.attr_name, self.__class__.attr_version, self.__class__.attr_author,
			            self.__class__.attr_description, self.__class__.attr_url, self.__class__.attr_license,
			            self.__class__.attr_pythoncompat):
				for a in reversed(assignments):
					targets = extract_target_ids(a)
					if key in targets:
						if isinstance(a.value, ast.Str):
							result[key] = a.value.s

						elif isinstance(a.value, ast.Call) and hasattr(a.value, "func") \
								and a.value.func.id == "gettext" and a.value.args \
								and isinstance(a.value.args[0], ast.Str):
							result[key] = a.value.args[0].s

						break

			for key in (self.__class__.attr_hidden,):
				for a in reversed(assignments):
					targets = extract_target_ids(a)
					if key in targets:
						if isinstance(a.value, ast.Name) and a.value.id in ("True", "False"):
							result[key] = bool(a.value.id)

						break

		except SyntaxError:
			self._logger.exception("Invalid syntax in {} for plugin {}".format(path, self.key))
			self.invalid_syntax = True
		except Exception:
			self._logger.exception("Error while parsing AST from {} for plugin {}".format(path, self.key))

		return result


class PluginManager(object):
	"""
	The :class:`PluginManager` is the central component for finding, loading and accessing plugins provided to the
	system.

	It is able to discover plugins both through possible file system locations as well as customizable entry points.
	"""

	def __init__(self, plugin_folders, plugin_bases, plugin_entry_points, logging_prefix=None,
	             plugin_disabled_list=None, plugin_blacklist=None, plugin_restart_needing_hooks=None,
	             plugin_obsolete_hooks=None, plugin_validators=None, compatibility_ignored_list=None):
		self.logger = logging.getLogger(__name__)

		if logging_prefix is None:
			logging_prefix = ""
		if plugin_folders is None:
			plugin_folders = []
		if plugin_bases is None:
			plugin_bases = []
		if plugin_entry_points is None:
			plugin_entry_points = []
		if plugin_disabled_list is None:
			plugin_disabled_list = []
		if plugin_blacklist is None:
			plugin_blacklist = []
		if compatibility_ignored_list is None:
			compatibility_ignored_list = []

		self.plugin_folders = plugin_folders
		self.plugin_bases = plugin_bases
		self.plugin_entry_points = plugin_entry_points
		self.plugin_disabled_list = plugin_disabled_list
		self.plugin_blacklist = plugin_blacklist
		self.plugin_restart_needing_hooks = plugin_restart_needing_hooks
		self.plugin_obsolete_hooks = plugin_obsolete_hooks
		self.plugin_validators = plugin_validators
		self.logging_prefix = logging_prefix
		self.compatibility_ignored_list = compatibility_ignored_list

		self.enabled_plugins = dict()
		self.disabled_plugins = dict()
		self.plugin_implementations = dict()
		self.plugin_implementations_by_type = defaultdict(list)

		self._plugin_hooks = defaultdict(list)

		self.implementation_injects = dict()
		self.implementation_inject_factories = []
		self.implementation_pre_inits = []
		self.implementation_post_inits = []

		self.on_plugin_loaded = lambda *args, **kwargs: None
		self.on_plugin_unloaded = lambda *args, **kwargs: None
		self.on_plugin_enabled = lambda *args, **kwargs: None
		self.on_plugin_disabled = lambda *args, **kwargs: None
		self.on_plugin_implementations_initialized = lambda *args, **kwargs: None

		self.on_plugins_loaded = lambda *args, **kwargs: None
		self.on_plugins_enabled = lambda *args, **kwargs: None

		self.registered_clients = []

		self.marked_plugins = defaultdict(list)

		self._python_install_dir = None
		self._python_virtual_env = False
		self._detect_python_environment()

	def _detect_python_environment(self):
		from distutils.command.install import install as cmd_install
		from distutils.dist import Distribution
		import sys

		cmd = cmd_install(Distribution())
		cmd.finalize_options()

		self._python_install_dir = cmd.install_lib
		self._python_prefix = os.path.realpath(sys.prefix)
		self._python_virtual_env = hasattr(sys, "real_prefix") \
		                           or (hasattr(sys, "base_prefix") and os.path.realpath(sys.prefix) != os.path.realpath(sys.base_prefix))

	@property
	def plugins(self):
		plugins = dict(self.enabled_plugins)
		plugins.update(self.disabled_plugins)
		return plugins

	@property
	def plugin_hooks(self):
		return {key: list(map(lambda v: (v[1], v[2]), value)) for key, value in self._plugin_hooks.items()}

	def find_plugins(self, existing=None, ignore_uninstalled=True, incl_all_found=False):
		added, found = self._find_plugins(existing=existing, ignore_uninstalled=ignore_uninstalled)
		if incl_all_found:
			return added, found
		else:
			return added

	def _find_plugins(self, existing=None, ignore_uninstalled=True):
		if existing is None:
			existing = dict(self.plugins)

		result_added = OrderedDict()
		result_found = []

		if self.plugin_folders:
			try:
				added, found = self._find_plugins_from_folders(self.plugin_folders,
				                                                 existing,
				                                                 ignored_uninstalled=ignore_uninstalled)
				result_added.update(added)
				result_found += found
			except Exception:
				self.logger.exception("Error fetching plugins from folders")

		if self.plugin_entry_points:
			existing.update(result_added)
			try:
				added, found = self._find_plugins_from_entry_points(self.plugin_entry_points,
				                                                      existing,
				                                                      ignore_uninstalled=ignore_uninstalled)
				result_added.update(added)
				result_found += found
			except Exception:
				self.logger.exception("Error fetching plugins from entry points")

		return result_added, result_found

	def _find_plugins_from_folders(self, folders, existing, ignored_uninstalled=True):
		added = OrderedDict()
		found = []

		for folder in folders:
			try:
				flagged_readonly = False
				package = None
				if isinstance(folder, (list, tuple)):
					if len(folder) == 2:
						folder, flagged_readonly = folder
					elif len(folder) == 3:
						folder, package, flagged_readonly = folder
					else:
						continue
				actual_readonly = not os.access(folder, os.W_OK)

				if not os.path.exists(folder):
					self.logger.warning("Plugin folder {folder} could not be found, skipping it".format(folder=folder))
					continue

				for entry in scandir(folder):
					try:
						if entry.is_dir():
							init_py = os.path.join(entry.path, "__init__.py")
							init_pyc = os.path.join(entry.path, "__init__.pyc")

							if not os.path.isfile(init_py) and not os.path.isfile(init_pyc):
								# neither does exist, we ignore this
								continue

							key = entry.name

						elif entry.is_file():
							key, ext = os.path.splitext(entry.name)
							if ext not in (".py", ".pyc") or key.startswith("__"):
								# neither py nor pyc, or starts with __ (like __init__), we ignore this
								continue

						else:
							# whatever this is, we ignore it
							continue

						found.append(key)
						if key in existing or key in added or (ignored_uninstalled and key in self.marked_plugins["uninstalled"]):
							# plugin is already defined, ignore it
							continue

						bundled = flagged_readonly

						module_name = None
						if package:
							module_name = "{}.{}".format(package, key)

						plugin = self._import_plugin_from_module(key, module_name=module_name, folder=folder, bundled=bundled)
						if plugin:
							if module_name:
								plugin.origin = ModuleOrigin("module", module_name, folder)
							else:
								plugin.origin = FolderOrigin("folder", folder)
							plugin.managable = not flagged_readonly and not actual_readonly
							plugin.enabled = False
							added[key] = plugin
					except Exception:
						self.logger.exception("Error processing folder entry {!r} from folder {}".format(entry, folder))
			except Exception:
				self.logger.exception("Error processing folder {}".format(folder))

		return added, found

	def _find_plugins_from_entry_points(self, groups, existing, ignore_uninstalled=True):
		added = OrderedDict()
		found = []

		# let's make sure we have a current working set ...
		working_set = pkg_resources.WorkingSet()

		# ... including the user's site packages
		import site
		import sys
		if site.ENABLE_USER_SITE:
			if not site.USER_SITE in working_set.entries:
				working_set.add_entry(site.USER_SITE)
			if not site.USER_SITE in sys.path:
				site.addsitedir(site.USER_SITE)

		if not isinstance(groups, (list, tuple)):
			groups = [groups]

		def wrapped(gen):
			# to protect against some issues in installed packages that make iteration over entry points
			# fall on its face - e.g. https://groups.google.com/forum/#!msg/octoprint/DyXdqhR0U7c/kKMUsMmIBgAJ
			for entry in gen:
				try:
					yield entry
				except Exception:
					self.logger.exception("Something went wrong while processing the entry points of a package in the "
					                      "Python environment - broken entry_points.txt in some package?")

		for group in groups:
			for entry_point in wrapped(working_set.iter_entry_points(group=group, name=None)):
				try:
					key = entry_point.name
					module_name = entry_point.module_name
					version = entry_point.dist.version

					found.append(key)
					if key in existing or key in added or (ignore_uninstalled and key in self.marked_plugins["uninstalled"]):
						# plugin is already defined or marked as uninstalled, ignore it
						continue

					kwargs = dict(module_name=module_name, version=version)
					package_name = entry_point.dist.project_name
					try:
						entry_point_metadata = EntryPointMetadata(entry_point)
					except Exception:
						self.logger.exception("Something went wrong while retrieving metadata for module {}".format(module_name))
					else:
						kwargs.update(dict(
							name=entry_point_metadata.name,
							summary=entry_point_metadata.summary,
							author=entry_point_metadata.author,
							url=entry_point_metadata.home_page,
							license=entry_point_metadata.license
						))

					plugin = self._import_plugin_from_module(key, **kwargs)
					if plugin:
						plugin.origin = EntryPointOrigin("entry_point", group, module_name, package_name, version)
						plugin.enabled = False

						# plugin is manageable if its location is writable and OctoPrint
						# is either not running from a virtual env or the plugin is
						# installed in that virtual env - the virtual env's pip will not
						# allow us to uninstall stuff that is installed outside
						# of the virtual env, so this check is necessary
						plugin.managable = os.access(plugin.location, os.W_OK) \
						                   and (not self._python_virtual_env
						                        or is_sub_path_of(plugin.location, self._python_prefix)
						                        or is_editable_install(self._python_install_dir,
						                                               package_name,
						                                               module_name,
						                                               plugin.location))

						added[key] = plugin
				except Exception:
					self.logger.exception("Error processing entry point {!r} for group {}".format(entry_point, group))

		return added, found

	def _import_plugin_from_module(self, key, folder=None, module_name=None, name=None, version=None, summary=None,
	                               author=None, url=None, license=None, bundled=False):
		# TODO error handling
		try:
			if folder:
				location, spec = _find_module(key, path=folder)
			elif module_name:
				location, spec = _find_module(module_name)
			else:
				return None
		except Exception:
			self.logger.exception("Could not locate plugin {key}".format(key=key))
			return None

		# Create a simple dummy entry first ...
		plugin = PluginInfo(key, location, None, name=name, version=version, description=summary, author=author,
		                    url=url, license=license)
		plugin.bundled = bundled

		if self._is_plugin_disabled(key):
			self.logger.info("Plugin {} is disabled.".format(plugin))
			plugin.forced_disabled = True

		if self._is_plugin_blacklisted(key) or (plugin.version is not None and self._is_plugin_version_blacklisted(key, plugin.version)):
			self.logger.warning("Plugin {} is blacklisted.".format(plugin))
			plugin.blacklisted = True

		python_version = get_python_version_string()
		if self._is_plugin_incompatible(key, plugin):
			if plugin.invalid_syntax:
				self.logger.warning("Plugin {} can't be compiled under Python {} due to invalid syntax".format(plugin, python_version))
			else:
				self.logger.warning("Plugin {} is not compatible to Python {} (compatibility string: {}).".format(plugin, python_version, plugin.pythoncompat))
			plugin.incompatible = True

		if not plugin.validate("before_import", additional_validators=self.plugin_validators):
			return plugin

		# ... then create and return the real one
		return self._import_plugin(key, spec,
		                           module_name=module_name, name=name, location=plugin.location, version=version,
		                           summary=summary, author=author, url=url, license=license, bundled=bundled,
		                           parsed_metadata=plugin.parsed_metadata)

	def _import_plugin(self, key, spec, module_name=None, name=None, location=None, version=None, summary=None, author=None, url=None, license=None, bundled=False, parsed_metadata=None):
		try:
			if module_name:
				module = _load_module(module_name, spec)
			else:
				module = _load_module(key, spec)

			plugin = PluginInfo(key, location, module,
			                    name=name,
			                    version=version,
			                    description=summary,
			                    author=author,
			                    url=url,
			                    license=license,
			                    parsed_metadata=parsed_metadata)

			plugin.bundled = bundled
		except Exception:
			self.logger.exception("Error loading plugin {key}".format(key=key))
			return None

		if plugin.check():
			return plugin
		else:
			self.logger.info("Plugin {plugin} did not pass check, not loading.".format(plugin=str(plugin)))
			return None

	def _is_plugin_disabled(self, key):
		return key in self.plugin_disabled_list or key.endswith('disabled')

	def _is_plugin_blacklisted(self, key):
		return key in self.plugin_blacklist

	def _is_plugin_incompatible(self, key, plugin):
		return not plugin.bundled \
		       and not is_python_compatible(plugin.pythoncompat) \
		       and key not in self.compatibility_ignored_list

	def _is_plugin_version_blacklisted(self, key, version):
		def matches_plugin(entry):
			if isinstance(entry, (tuple, list)) and len(entry) == 2:
				entry_key, entry_version = entry
				return entry_key == key and entry_version == version
			return False

		return any(map(lambda entry: matches_plugin(entry),
		               self.plugin_blacklist))

	def reload_plugins(self, startup=False, initialize_implementations=True, force_reload=None):
		self.logger.info("Loading plugins from {folders} and installed plugin packages...".format(
			folders=", ".join(map(lambda x: x[0] if isinstance(x, tuple) else str(x), self.plugin_folders))
		))

		if force_reload is None:
			force_reload = []

		added, found = self.find_plugins(existing=dict((k, v) for k, v in self.plugins.items() if not k in force_reload),
		                                 incl_all_found=True)

		# let's clean everything we DIDN'T find first
		removed = [key for key in list(self.enabled_plugins.keys()) + list(self.disabled_plugins.keys()) if key not in found]
		for key in removed:
			try:
				del self.enabled_plugins[key]
			except KeyError:
				pass

			try:
				del self.disabled_plugins[key]
			except KeyError:
				pass

		self.disabled_plugins.update(added)

		# 1st pass: loading the plugins
		for name, plugin in added.items():
			try:
				if not plugin.blacklisted and not plugin.forced_disabled and not plugin.incompatible:
					self.load_plugin(name, plugin, startup=startup, initialize_implementation=initialize_implementations)
			except PluginNeedsRestart:
				pass
			except PluginLifecycleException as e:
				self.logger.info(str(e))

		self.on_plugins_loaded(startup=startup,
							   initialize_implementations=initialize_implementations,
							   force_reload=force_reload)

		# 2nd pass: enabling those plugins that need enabling
		for name, plugin in added.items():
			try:
				if plugin.loaded and not plugin.forced_disabled and not plugin.incompatible:
					if plugin.blacklisted:
						self.logger.warning("Plugin {} is blacklisted. Not enabling it.".format(plugin))
						continue
					self.enable_plugin(name, plugin=plugin, initialize_implementation=initialize_implementations, startup=startup)
			except PluginNeedsRestart:
				pass
			except PluginLifecycleException as e:
				self.logger.info(str(e))

		self.on_plugins_enabled(startup=startup,
								initialize_implementations=initialize_implementations,
								force_reload=force_reload)

		if len(self.enabled_plugins) <= 0:
			self.logger.info("No plugins found")
		else:
			self.logger.info("Found {count} plugin(s) providing {implementations} mixin implementations, {hooks} hook handlers".format(
				count=len(self.enabled_plugins) + len(self.disabled_plugins),
				implementations=len(self.plugin_implementations),
				hooks=sum(map(lambda x: len(x), self.plugin_hooks.values()))
			))

	def mark_plugin(self, name, **kwargs):
		if not name in self.plugins:
			self.logger.debug("Trying to mark an unknown plugin {name}".format(**locals()))

		for key, value in kwargs.items():
			if value is None:
				continue

			if value and not name in self.marked_plugins[key]:
				self.marked_plugins[key].append(name)
			elif not value and name in self.marked_plugins[key]:
				self.marked_plugins[key].remove(name)

	def is_plugin_marked(self, name, key):
		if not name in self.plugins:
			return False

		return name in self.marked_plugins[key]

	def load_plugin(self, name, plugin=None, startup=False, initialize_implementation=True):
		if not name in self.plugins:
			self.logger.warning("Trying to load an unknown plugin {name}".format(**locals()))
			return

		if plugin is None:
			plugin = self.plugins[name]

		try:
			if not plugin.validate("before_load", additional_validators=self.plugin_validators):
				return

			plugin.load()
			plugin.validate("after_load", additional_validators=self.plugin_validators)
			self.on_plugin_loaded(name, plugin)
			plugin.loaded = True

			self.logger.debug("Loaded plugin {name}: {plugin}".format(**locals()))
		except PluginLifecycleException as e:
			raise e
		except Exception:
			self.logger.exception("There was an error loading plugin %s" % name)

	def unload_plugin(self, name):
		if not name in self.plugins:
			self.logger.warning("Trying to unload unknown plugin {name}".format(**locals()))
			return

		plugin = self.plugins[name]

		try:
			if plugin.enabled:
				self.disable_plugin(name, plugin=plugin)

			plugin.unload()
			self.on_plugin_unloaded(name, plugin)

			if name in self.enabled_plugins:
				del self.enabled_plugins[name]

			if name in self.disabled_plugins:
				del self.disabled_plugins[name]

			plugin.loaded = False

			self.logger.debug("Unloaded plugin {name}: {plugin}".format(**locals()))
		except PluginLifecycleException as e:
			raise e
		except Exception:
			self.logger.exception("There was an error unloading plugin {name}".format(**locals()))

			# make sure the plugin is NOT in the list of enabled plugins but in the list of disabled plugins
			if name in self.enabled_plugins:
				del self.enabled_plugins[name]
			if not name in self.disabled_plugins:
				self.disabled_plugins[name] = plugin

	def enable_plugin(self, name, plugin=None, initialize_implementation=True, startup=False):
		if not name in self.disabled_plugins:
			self.logger.warning("Tried to enable plugin {name}, however it is not disabled".format(**locals()))
			return

		if plugin is None:
			plugin = self.disabled_plugins[name]

		if not startup and self.is_restart_needing_plugin(plugin):
			raise PluginNeedsRestart(name)

		if self.has_obsolete_hooks(plugin):
			raise PluginCantEnable(name, "Dependency on obsolete hooks detected, full functionality cannot be guaranteed")

		try:
			if not plugin.validate("before_enable", additional_validators=self.plugin_validators):
				return False

			plugin.enable()
			self._activate_plugin(name, plugin)
		except PluginLifecycleException as e:
			raise e
		except Exception:
			self.logger.exception("There was an error while enabling plugin {name}".format(**locals()))
			return False
		else:
			if name in self.disabled_plugins:
				del self.disabled_plugins[name]
			self.enabled_plugins[name] = plugin
			plugin.enabled = True

			if plugin.implementation:
				if initialize_implementation:
					if not self.initialize_implementation_of_plugin(name, plugin):
						return False
				plugin.implementation.on_plugin_enabled()
			self.on_plugin_enabled(name, plugin)

			self.logger.debug("Enabled plugin {name}: {plugin}".format(**locals()))

		return True

	def disable_plugin(self, name, plugin=None):
		if not name in self.enabled_plugins:
			self.logger.warning("Tried to disable plugin {name}, however it is not enabled".format(**locals()))
			return

		if plugin is None:
			plugin = self.enabled_plugins[name]

		if self.is_restart_needing_plugin(plugin):
			raise PluginNeedsRestart(name)

		try:
			plugin.disable()
			self._deactivate_plugin(name, plugin)
		except PluginLifecycleException as e:
			raise e
		except Exception:
			self.logger.exception("There was an error while disabling plugin {name}".format(**locals()))
			return False
		else:
			if name in self.enabled_plugins:
				del self.enabled_plugins[name]
			self.disabled_plugins[name] = plugin
			plugin.enabled = False

			if plugin.implementation:
				plugin.implementation.on_plugin_disabled()
			self.on_plugin_disabled(name, plugin)

			self.logger.debug("Disabled plugin {name}: {plugin}".format(**locals()))

		return True

	def _activate_plugin(self, name, plugin):
		plugin.hotchangeable = self.is_restart_needing_plugin(plugin)

		# evaluate registered hooks
		for hook, definition in plugin.hooks.items():
			try:
				callback, order = self._get_callback_and_order(definition)
			except ValueError as e:
				self.logger.warning("There is something wrong with the hook definition {} for plugin {}: {}".format(definition, name, str(e)))
				continue

			self._plugin_hooks[hook].append((order, name, callback))
			self._sort_hooks(hook)

		# evaluate registered implementation
		if plugin.implementation:
			mixins = self.mixins_matching_bases(plugin.implementation.__class__, *self.plugin_bases)
			for mixin in mixins:
				self.plugin_implementations_by_type[mixin].append((name, plugin.implementation))

			self.plugin_implementations[name] = plugin.implementation

	def _deactivate_plugin(self, name, plugin):
		for hook, definition in plugin.hooks.items():
			try:
				callback, order = self._get_callback_and_order(definition)
			except ValueError as e:
				self.logger.warning("There is something wrong with the hook definition {} for plugin {}: {}".format(definition, name, str(e)))
				continue

			try:
				self._plugin_hooks[hook].remove((order, name, callback))
				self._sort_hooks(hook)
			except ValueError:
				# that's ok, the plugin was just not registered for the hook
				pass

		if plugin.implementation is not None:
			if name in self.plugin_implementations:
				del self.plugin_implementations[name]

			mixins = self.mixins_matching_bases(plugin.implementation.__class__, *self.plugin_bases)
			for mixin in mixins:
				try:
					self.plugin_implementations_by_type[mixin].remove((name, plugin.implementation))
				except ValueError:
					# that's ok, the plugin was just not registered for the type
					pass

	def is_restart_needing_plugin(self, plugin):
		return plugin.needs_restart or self.has_restart_needing_implementation(plugin) or self.has_restart_needing_hooks(plugin)

	def has_restart_needing_implementation(self, plugin):
		return self.has_any_of_mixins(plugin, RestartNeedingPlugin)

	def has_restart_needing_hooks(self, plugin):
		return self.has_any_of_hooks(plugin, self.plugin_restart_needing_hooks)

	def has_obsolete_hooks(self, plugin):
		return self.has_any_of_hooks(plugin, self.plugin_obsolete_hooks)

	def is_restart_needing_hook(self, hook):
		return self.hook_matches_hooks(hook, self.plugin_restart_needing_hooks)

	def is_obsolete_hook(self, hook):
		return self.hook_matches_hooks(hook, self.plugin_obsolete_hooks)

	@staticmethod
	def has_any_of_hooks(plugin, *hooks):
		"""
		Tests if the ``plugin`` contains any of the provided ``hooks``.

		Uses :func:`octoprint.plugin.core.PluginManager.hook_matches_hooks`.

		Args:
			plugin: plugin to test hooks for
			*hooks: hooks to test against

		Returns:
			(bool): True if any of the plugin's hooks match the provided hooks,
			        False otherwise.
		"""

		if hooks and len(hooks) == 1 and isinstance(hooks[0], (list, tuple)):
			hooks = hooks[0]

		hooks = list(filter(lambda hook: hook is not None, hooks))
		if not hooks:
			return False
		if not plugin or not plugin.hooks:
			return False

		plugin_hooks = plugin.hooks.keys()

		return any(map(lambda hook: PluginManager.hook_matches_hooks(hook, *hooks),
		               plugin_hooks))

	@staticmethod
	def hook_matches_hooks(hook, *hooks):
		"""
		Tests if ``hook`` matches any of the provided ``hooks`` to test for.

		``hook`` is expected to be an exact hook name.

		``hooks`` is expected to be a list containing one or more hook names or
		patterns. That can be either an exact hook name or an
		:func:`fnmatch.fnmatch` pattern.

		Args:
			hook: the hook to test
			hooks: the hook name patterns to test against

		Returns:
			(bool): True if the ``hook`` matches any of the ``hooks``, False otherwise.

		"""

		if hooks and len(hooks) == 1 and isinstance(hooks[0], (list, tuple)):
			hooks = hooks[0]

		hooks = list(filter(lambda hook: hook is not None, hooks))
		if not hooks:
			return False
		if not hook:
			return False

		return any(map(lambda h: fnmatch.fnmatch(hook, h),
		               hooks))

	@staticmethod
	def mixins_matching_bases(klass, *bases):
		result = set()
		for c in inspect.getmro(klass):
			if c == klass or c in bases:
				# ignore the exact class and our bases
				continue
			if issubclass(c, bases):
				result.add(c)
		return result

	@staticmethod
	def has_any_of_mixins(plugin, *mixins):
		"""
		Tests if the ``plugin`` has an implementation implementing any
		of the provided ``mixins``.

		Args:
			plugin: plugin for which to check the implementation
			*mixins: mixins to test against

		Returns:
			(bool): True if the plugin's implementation implements any of the
			        provided mixins, False otherwise.
		"""

		if mixins and len(mixins) == 1 and isinstance(mixins[0], (list, tuple)):
			mixins = mixins[0]

		mixins = list(filter(lambda mixin: mixin is not None, mixins))
		if not mixins:
			return False
		if not plugin or not plugin.implementation:
			return False

		return isinstance(plugin.implementation, tuple(mixins))

	def initialize_implementations(self, additional_injects=None, additional_inject_factories=None, additional_pre_inits=None, additional_post_inits=None):
		for name, plugin in self.enabled_plugins.items():
			self.initialize_implementation_of_plugin(name, plugin,
			                                         additional_injects=additional_injects,
			                                         additional_inject_factories=additional_inject_factories,
			                                         additional_pre_inits=additional_pre_inits,
			                                         additional_post_inits=additional_post_inits)

		self.logger.info("Initialized {count} plugin implementation(s)".format(count=len(self.plugin_implementations)))

	def initialize_implementation_of_plugin(self, name, plugin, additional_injects=None, additional_inject_factories=None, additional_pre_inits=None, additional_post_inits=None):
		if plugin.implementation is None:
			return

		return self.initialize_implementation(name, plugin, plugin.implementation,
		                               additional_injects=additional_injects,
		                               additional_inject_factories=additional_inject_factories,
		                               additional_pre_inits=additional_pre_inits,
		                               additional_post_inits=additional_post_inits)

	def initialize_implementation(self, name, plugin, implementation, additional_injects=None, additional_inject_factories=None, additional_pre_inits=None, additional_post_inits=None):
		if additional_injects is None:
			additional_injects = dict()
		if additional_inject_factories is None:
			additional_inject_factories = []
		if additional_pre_inits is None:
			additional_pre_inits = []
		if additional_post_inits is None:
			additional_post_inits = []

		injects = self.implementation_injects
		injects.update(additional_injects)

		inject_factories = self.implementation_inject_factories
		inject_factories += additional_inject_factories

		pre_inits = self.implementation_pre_inits
		pre_inits += additional_pre_inits

		post_inits = self.implementation_post_inits
		post_inits += additional_post_inits

		try:
			kwargs = dict(injects)

			kwargs.update(dict(
				identifier=name,
				plugin_name=plugin.name,
				plugin_version=plugin.version,
				plugin_info=plugin,
				basefolder=os.path.realpath(plugin.location),
				logger=logging.getLogger(self.logging_prefix + name),
				))

			# inject the additional_injects
			for arg, value in kwargs.items():
				setattr(implementation, "_" + arg, value)

			# inject any injects produced in the additional_inject_factories
			for factory in inject_factories:
				try:
					return_value = factory(name, implementation)
				except Exception:
					self.logger.exception("Exception while executing injection factory %r" % factory)
				else:
					if return_value is not None:
						if isinstance(return_value, dict):
							for arg, value in return_value.items():
								setattr(implementation, "_" + arg, value)

			# execute any additional pre init methods
			for pre_init in pre_inits:
				pre_init(name, implementation)

			implementation.initialize()

			# execute any additional post init methods
			for post_init in post_inits:
				post_init(name, implementation)

		except Exception as e:
			self._deactivate_plugin(name, plugin)
			plugin.enabled = False

			if isinstance(e, PluginLifecycleException):
				raise e
			else:
				self.logger.exception("Exception while initializing plugin {name}, disabling it".format(**locals()))
				return False
		else:
			self.on_plugin_implementations_initialized(name, plugin)

		self.logger.debug("Initialized plugin mixin implementation for plugin {name}".format(**locals()))
		return True


	def log_all_plugins(self, show_bundled=True, bundled_str=(" (bundled)", ""), show_location=True,
	                    location_str=" = {location}", show_enabled=True, enabled_str=(" ", "!", "#", "*"),
	                    only_to_handler=None):
		all_plugins = list(self.enabled_plugins.values()) + list(self.disabled_plugins.values())

		def _log(message, level=logging.INFO):
			if only_to_handler is not None:
				import octoprint.logging
				octoprint.logging.log_to_handler(self.logger, only_to_handler, level, message, [])
			else:
				self.logger.log(level, message)

		if len(all_plugins) <= 0:
			_log("No plugins available")
		else:
			formatted_plugins = "\n".join(map(lambda x: "| " + x.long_str(show_bundled=show_bundled,
				                                                          bundled_strs=bundled_str,
				                                                          show_location=show_location,
				                                                          location_str=location_str,
				                                                          show_enabled=show_enabled,
				                                                          enabled_strs=enabled_str),
				                              sorted(self.plugins.values(), key=lambda x: unicode(x).lower())))
			legend = "Prefix legend: {1} = disabled, {2} = blacklisted, {3} = incompatible".format(*enabled_str)
			_log("{count} plugin(s) registered with the system:\n{plugins}\n{legend}".format(count=len(all_plugins),
			                                                                                 plugins=formatted_plugins,
			                                                                                 legend=legend))

	def get_plugin(self, identifier, require_enabled=True):
		"""
		Retrieves the module of the plugin identified by ``identifier``. If the plugin is not registered or disabled and
		``required_enabled`` is True (the default) None will be returned.

		Arguments:
		    identifier (str): The identifier of the plugin to retrieve.
		    require_enabled (boolean): Whether to only return the plugin if is enabled (True, default) or also if it's
		        disabled.

		Returns:
		    module: The requested plugin module or None
		"""

		plugin_info = self.get_plugin_info(identifier, require_enabled=require_enabled)
		if plugin_info is not None:
			return plugin_info.instance
		return None

	def get_plugin_info(self, identifier, require_enabled=True):
		"""
		Retrieves the :class:`PluginInfo` instance identified by ``identifier``. If the plugin is not registered or
		disabled and ``required_enabled`` is True (the default) None will be returned.

		Arguments:
		    identifier (str): The identifier of the plugin to retrieve.
		    require_enabled (boolean): Whether to only return the plugin if is enabled (True, default) or also if it's
		        disabled.

		Returns:
		    ~.PluginInfo: The requested :class:`PluginInfo` or None
		"""

		if identifier in self.enabled_plugins:
			return self.enabled_plugins[identifier]
		elif not require_enabled and identifier in self.disabled_plugins:
			return self.disabled_plugins[identifier]

		return None

	def get_hooks(self, hook):
		"""
		Retrieves all registered handlers for the specified hook.

		Arguments:
		    hook (str): The hook for which to retrieve the handlers.

		Returns:
		    dict: A dict containing all registered handlers mapped by their plugin's identifier.
		"""

		if not hook in self.plugin_hooks:
			return dict()

		result = OrderedDict()
		for h in self.plugin_hooks[hook]:
			result[h[0]] = h[1]
		return result

	def get_implementations(self, *types, **kwargs):
		"""
		Get all mixin implementations that implement *all* of the provided ``types``.

		Arguments:
		    types (one or more type): The types a mixin implementation needs to implement in order to be returned.

		Returns:
		    list: A list of all found implementations
		"""

		sorting_context = kwargs.get("sorting_context", None)

		result = None

		for t in types:
			implementations = self.plugin_implementations_by_type[t]
			if result is None:
				result = set(implementations)
			else:
				result = result.intersection(implementations)

		if result is None:
			return []

		def sort_func(impl):
			sorting_value = None
			if sorting_context is not None and isinstance(impl[1], SortablePlugin):
				try:
					sorting_value = impl[1].get_sorting_key(sorting_context)
				except Exception:
					self.logger.exception("Error while trying to retrieve sorting order for plugin {}".format(impl[0]))

				if sorting_value is not None:
					try:
						sorting_value = int(sorting_value)
					except ValueError:
						self.logger.warning("The order value returned by {} for sorting context {} is not a valid integer, ignoring it".format(impl[0], sorting_context))
						sorting_value = None

			plugin_info = self.get_plugin_info(impl[0], require_enabled=False)
			return sorting_value is None, sv(sorting_value), not plugin_info.bundled if plugin_info else True, sv(impl[0])

		return [impl[1] for impl in sorted(result, key=sort_func)]

	def get_filtered_implementations(self, f, *types, **kwargs):
		"""
		Get all mixin implementations that implement *all* of the provided ``types`` and match the provided filter `f`.

		Arguments:
		    f (callable): A filter function returning True for implementations to return and False for those to exclude.
		    types (one or more type): The types a mixin implementation needs to implement in order to be returned.

		Returns:
		    list: A list of all found and matching implementations.
		"""

		assert callable(f)
		implementations = self.get_implementations(*types, sorting_context=kwargs.get("sorting_context", None))
		return list(filter(f, implementations))

	def get_helpers(self, name, *helpers):
		"""
		Retrieves the named ``helpers`` for the plugin with identifier ``name``.

		If the plugin is not available, returns None. Otherwise returns a :class:`dict` with the requested plugin
		helper names mapped to the method - if a helper could not be resolved, it will be missing from the dict.

		Arguments:
		    name (str): Identifier of the plugin for which to look up the ``helpers``.
		    helpers (one or more str): Identifiers of the helpers of plugin ``name`` to return.

		Returns:
		    dict: A dictionary of all resolved helpers, mapped by their identifiers, or None if the plugin was not
		        registered with the system.
		"""

		if not name in self.enabled_plugins:
			return None
		plugin = self.enabled_plugins[name]

		all_helpers = plugin.helpers
		if len(helpers):
			return dict((k, v) for (k, v) in all_helpers.items() if k in helpers)
		else:
			return all_helpers

	def register_message_receiver(self, client):
		"""
		Registers a ``client`` for receiving plugin messages. The ``client`` needs to be a callable accepting two
		input arguments, ``plugin`` (the sending plugin's identifier) and ``data`` (the message itself), and one
		optional keyword argument, ``permissions`` (an optional list of permissions to test against).
		"""

		if client is None:
			return
		self.registered_clients.append(client)

	def unregister_message_receiver(self, client):
		"""
		Unregisters a ``client`` for receiving plugin messages.
		"""

		try:
			self.registered_clients.remove(client)
		except ValueError:
			# not registered
			pass

	def send_plugin_message(self, plugin, data, permissions=None):
		"""
		Sends ``data`` in the name of ``plugin`` to all currently registered message receivers by invoking them
		with the three arguments.

		Arguments:
		    plugin (str): The sending plugin's identifier.
		    data (object): The message.
		    permissions (list): A list of permissions to test against in the client.
		"""

		for client in self.registered_clients:
			try:
				client(plugin, data, permissions=permissions)
			except Exception:
				self.logger.exception("Exception while sending plugin data to client")

	def _sort_hooks(self, hook):
		self._plugin_hooks[hook] = sorted(self._plugin_hooks[hook],
		                                  key=lambda x: (x[0] is None, sv(x[0]), sv(x[1]), sv(x[2])))

	def _get_callback_and_order(self, hook):
		if callable(hook):
			return hook, None

		elif isinstance(hook, tuple) and len(hook) == 2:
			callback, order = hook

			# test that callback is a callable
			if not callable(callback):
				raise ValueError("Hook callback is not a callable")

			# test that number is an int
			try:
				int(order)
			except ValueError:
				raise ValueError("Hook order is not a number")

			return callback, order

		else:
			raise ValueError("Invalid hook definition, neither a callable nor a 2-tuple (callback, order): {!r}".format(hook))


def is_sub_path_of(path, parent):
	"""
	Tests if `path` is a sub path (or identical) to `path`.

	>>> is_sub_path_of("/a/b/c", "/a/b")
	True
	>>> is_sub_path_of("/a/b/c", "/a/b2")
	False
	>>> is_sub_path_of("/a/b/c", "/b/c")
	False
	>>> is_sub_path_of("/foo/bar/../../a/b/c", "/a/b")
	True
	>>> is_sub_path_of("/a/b", "/a/b")
	True
	"""
	rel_path = os.path.relpath(os.path.realpath(path),
	                           os.path.realpath(parent))
	return not (rel_path == os.pardir or
	            rel_path.startswith(os.pardir + os.sep))


def is_editable_install(install_dir, package, module, location):
	package_link = os.path.join(install_dir, "{}.egg-link".format(package))
	if os.path.isfile(package_link):
		expected_target = os.path.normcase(os.path.realpath(location))
		try:
			with io.open(package_link, 'rt', encoding='utf-8') as f:
				contents = f.readlines()
			for line in contents:
				target = os.path.normcase(os.path.realpath(os.path.join(line.strip(), module)))
				if target == expected_target:
					return True
		except Exception:
			raise # TODO really ignore this?
			pass
	return False


class EntryPointMetadata(pkginfo.Distribution):
	def __init__(self, entry_point):
		self.entry_point = entry_point
		self.extractMetadata()

	def read(self):
		import warnings

		metadata_files = ("METADATA",  # wheel
		                  "PKG-INFO")  # egg

		if self.entry_point and self.entry_point.dist:
			for metadata_file in metadata_files:
				try:
					return self.entry_point.dist.get_metadata(metadata_file)
				except (IOError, OSError):
					# file not found, metadata file might be missing, ignore
					# IOError: file not found in Py2
					# OSError (specifically FileNotFoundError): file not found in Py3
					pass

		warnings.warn('No package metadata found for package {}'.format(self.entry_point.module_name))


class Plugin(object):
	"""
	The parent class of all plugin implementations.

	.. attribute:: _identifier

	   The identifier of the plugin. Injected by the plugin core system upon initialization of the implementation.

	.. attribute:: _plugin_name

	   The name of the plugin. Injected by the plugin core system upon initialization of the implementation.

	.. attribute:: _plugin_version

	   The version of the plugin. Injected by the plugin core system upon initialization of the implementation.

	.. attribute:: _basefolder

	   The base folder of the plugin. Injected by the plugin core system upon initialization of the implementation.

	.. attribute:: _logger

	   The logger instance to use, with the logging name set to the :attr:`PluginManager.logging_prefix` of the
	   :class:`PluginManager` concatenated with :attr:`_identifier`. Injected by the plugin core system upon
	   initialization of the implementation.
	"""

	def __init__(self):
		self._identifier = None
		self._plugin_name = None
		self._plugin_version = None
		self._basefolder = None
		self._logger = None

	def initialize(self):
		"""
		Called by the plugin core after performing all injections. Override this to initialize your implementation.
		"""
		pass

	def on_plugin_enabled(self):
		pass

	def on_plugin_disabled(self):
		pass

class RestartNeedingPlugin(Plugin):
	"""
	Mixin for plugin types that need a restart after enabling/disabling them.
	"""

class SortablePlugin(Plugin):
	"""
	Mixin for plugin types that are sortable.
	"""

	def get_sorting_key(self, context=None):
		"""
		Returns the sorting key to use for the implementation in the specified ``context``.

		May return ``None`` if order is irrelevant.

		Implementations returning None will be ordered by plugin identifier
		after all implementations which did return a sorting key value that was
		not None sorted by that.

		Arguments:
		    context (str): The sorting context for which to provide the
		        sorting key value.

		Returns:
		    int or None: An integer signifying the sorting key value of the plugin
		        (sorting will be done ascending), or None if the implementation
		        doesn't care about calling order.
		"""
		return None

class PluginNeedsRestart(Exception):
	def __init__(self, name):
		Exception.__init__(self)
		self.name = name
		self.message = "Plugin {name} cannot be enabled or disabled after system startup".format(**locals())

class PluginLifecycleException(Exception):
	def __init__(self, name, reason, message):
		Exception.__init__(self)
		self.name = name
		self.reason = reason

		self.message = message.format(**locals())

	def __str__(self):
		return self.message

class PluginCantInitialize(PluginLifecycleException):
	def __init__(self, name, reason):
		PluginLifecycleException.__init__(self, name, reason, "Plugin {name} cannot be initialized: {reason}")

class PluginCantEnable(PluginLifecycleException):
	def __init__(self, name, reason):
		PluginLifecycleException.__init__(self, name, reason, "Plugin {name} cannot be enabled: {reason}")

class PluginCantDisable(PluginLifecycleException):
	def __init__(self, name, reason):
		PluginLifecycleException.__init__(self, name, reason, "Plugin {name} cannot be disabled: {reason}")
