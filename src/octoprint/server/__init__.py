# coding=utf-8
from __future__ import absolute_import

__author__ = "Gina Häußge <osd@foosel.net>"
__license__ = 'GNU Affero General Public License http://www.gnu.org/licenses/agpl.html'
__copyright__ = "Copyright (C) 2014 The OctoPrint Project - Released under terms of the AGPLv3 License"

import uuid
from sockjs.tornado import SockJSRouter
from flask import Flask, g, request, session
from flask.ext.login import LoginManager, current_user
from flask.ext.principal import Principal, Permission, RoleNeed, identity_loaded, UserNeed
from flask.ext.babel import Babel, gettext, ngettext
from babel import Locale
from watchdog.observers import Observer
from collections import defaultdict

import os
import logging
import logging.config
import atexit

SUCCESS = {}
NO_CONTENT = ("", 204)

app = Flask("octoprint")
babel = None
debug = False

printer = None
printerProfileManager = None
fileManager = None
slicingManager = None
analysisQueue = None
userManager = None
eventManager = None
loginManager = None
pluginManager = None
appSessionManager = None

principals = Principal(app)
admin_permission = Permission(RoleNeed("admin"))
user_permission = Permission(RoleNeed("user"))

# only import the octoprint stuff down here, as it might depend on things defined above to be initialized already
from octoprint.printer import get_connection_options
from octoprint.printer.profile import PrinterProfileManager
from octoprint.printer.standard import Printer
from octoprint.settings import settings
import octoprint.users as users
import octoprint.events as events
import octoprint.plugin
import octoprint.timelapse
import octoprint._version
import octoprint.util
import octoprint.filemanager.storage
import octoprint.filemanager.analysis
import octoprint.slicing

from . import util

UI_API_KEY = ''.join('%02X' % ord(z) for z in uuid.uuid4().bytes)

versions = octoprint._version.get_versions()
VERSION = versions['version']
BRANCH = versions['branch'] if 'branch' in versions else None
DISPLAY_VERSION = "%s (%s branch)" % (VERSION, BRANCH) if BRANCH else VERSION
del versions

LOCALES = []
LANGUAGES = set()

@identity_loaded.connect_via(app)
def on_identity_loaded(sender, identity):
	user = load_user(identity.id)
	if user is None:
		return

	identity.provides.add(UserNeed(user.get_name()))
	if user.is_user():
		identity.provides.add(RoleNeed("user"))
	if user.is_admin():
		identity.provides.add(RoleNeed("admin"))

def load_user(id):
	if id == "_api":
		return users.ApiUser()

	if session and "usersession.id" in session:
		sessionid = session["usersession.id"]
	else:
		sessionid = None

	if userManager is not None:
		if sessionid:
			return userManager.findUser(username=id, session=sessionid)
		else:
			return userManager.findUser(username=id)
	return users.DummyUser()


#~~ startup code


class Server():
	def __init__(self, configfile=None, basedir=None, host="0.0.0.0", port=5000, debug=False, allowRoot=False, logConf=None):
		self._configfile = configfile
		self._basedir = basedir
		self._host = host
		self._port = port
		self._debug = debug
		self._allowRoot = allowRoot
		self._logConf = logConf
		self._server = None

		self._logger = None

		self._lifecycle_callbacks = defaultdict(list)

		self._template_searchpaths = []

	def run(self):
		if not self._allowRoot:
			self._check_for_root()

		global app
		global babel

		global printer
		global printerProfileManager
		global fileManager
		global slicingManager
		global analysisQueue
		global userManager
		global eventManager
		global loginManager
		global pluginManager
		global appSessionManager
		global debug

		from tornado.ioloop import IOLoop
		from tornado.web import Application, RequestHandler

		import sys

		debug = self._debug

		# first initialize the settings singleton and make sure it uses given configfile and basedir if available
		s = settings(init=True, basedir=self._basedir, configfile=self._configfile)

		# then monkey patch a bunch of stuff
		util.tornado.fix_ioloop_scheduling()
		util.flask.enable_additional_translations(additional_folders=[s.getBaseFolder("translations")])

		# setup app
		self._setup_app()

		# setup i18n
		self._setup_i18n(app)

		# then initialize logging
		self._setup_logging(self._debug, self._logConf)
		self._logger = logging.getLogger(__name__)
		def exception_logger(exc_type, exc_value, exc_tb):
			self._logger.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
		sys.excepthook = exception_logger
		self._logger.info("Starting OctoPrint %s" % DISPLAY_VERSION)

		# then initialize the plugin manager
		pluginManager = octoprint.plugin.plugin_manager(init=True)

		printerProfileManager = PrinterProfileManager()
		eventManager = events.eventManager()
		analysisQueue = octoprint.filemanager.analysis.AnalysisQueue()
		slicingManager = octoprint.slicing.SlicingManager(s.getBaseFolder("slicingProfiles"), printerProfileManager)
		storage_managers = dict()
		storage_managers[octoprint.filemanager.FileDestinations.LOCAL] = octoprint.filemanager.storage.LocalFileStorage(s.getBaseFolder("uploads"))
		fileManager = octoprint.filemanager.FileManager(analysisQueue, slicingManager, printerProfileManager, initial_storage_managers=storage_managers)
		printer = Printer(fileManager, analysisQueue, printerProfileManager)
		appSessionManager = util.flask.AppSessionManager()
		pluginLifecycleManager = LifecycleManager(pluginManager)

		def octoprint_plugin_inject_factory(name, implementation):
			if not isinstance(implementation, octoprint.plugin.OctoPrintPlugin):
				return None
			return dict(
				plugin_manager=pluginManager,
				printer_profile_manager=printerProfileManager,
				event_bus=eventManager,
				analysis_queue=analysisQueue,
				slicing_manager=slicingManager,
				file_manager=fileManager,
				printer=printer,
				app_session_manager=appSessionManager,
				plugin_lifecycle_manager=pluginLifecycleManager
			)

		def settings_plugin_inject_factory(name, implementation):
			if not isinstance(implementation, octoprint.plugin.SettingsPlugin):
				return None
			default_settings = implementation.get_settings_defaults()
			get_preprocessors, set_preprocessors = implementation.get_settings_preprocessors()
			plugin_settings = octoprint.plugin.plugin_settings(name,
			                                                   defaults=default_settings,
			                                                   get_preprocessors=get_preprocessors,
			                                                   set_preprocessors=set_preprocessors)
			return dict(settings=plugin_settings)

		pluginManager.implementation_inject_factories=[octoprint_plugin_inject_factory, settings_plugin_inject_factory]
		pluginManager.initialize_implementations()

		pluginManager.log_all_plugins()

		# initialize file manager and register it for changes in the registered plugins
		fileManager.initialize()
		pluginLifecycleManager.add_callback(["enabled", "disabled"], lambda name, plugin: fileManager.reload_plugins())

		# initialize slicing manager and register it for changes in the registered plugins
		slicingManager.initialize()
		pluginLifecycleManager.add_callback(["enabled", "disabled"], lambda name, plugin: slicingManager.reload_slicers())

		# setup jinja2
		self._setup_jinja2()
		def template_enabled(name, plugin):
			if plugin.implementation is None or not isinstance(plugin.implementation, octoprint.plugin.TemplatePlugin):
				return
			self._register_additional_template_plugin(plugin.implementation)
		def template_disabled(name, plugin):
			if plugin.implementation is None or not isinstance(plugin.implementation, octoprint.plugin.TemplatePlugin):
				return
			self._unregister_additional_template_plugin(plugin.implementation)
		pluginLifecycleManager.add_callback("enabled", template_enabled)
		pluginLifecycleManager.add_callback("disabled", template_disabled)

		# configure timelapse
		octoprint.timelapse.configureTimelapse()

		# setup command triggers
		events.CommandTrigger(printer)
		if self._debug:
			events.DebugEventListener()

		if s.getBoolean(["accessControl", "enabled"]):
			userManagerName = s.get(["accessControl", "userManager"])
			try:
				clazz = octoprint.util.get_class(userManagerName)
				userManager = clazz()
			except AttributeError, e:
				self._logger.exception("Could not instantiate user manager %s, will run with accessControl disabled!" % userManagerName)

		app.wsgi_app = util.ReverseProxied(
			app.wsgi_app,
			s.get(["server", "reverseProxy", "prefixHeader"]),
			s.get(["server", "reverseProxy", "schemeHeader"]),
			s.get(["server", "reverseProxy", "hostHeader"]),
			s.get(["server", "reverseProxy", "prefixFallback"]),
			s.get(["server", "reverseProxy", "schemeFallback"]),
			s.get(["server", "reverseProxy", "hostFallback"])
		)

		secret_key = s.get(["server", "secretKey"])
		if not secret_key:
			import string
			from random import choice
			chars = string.ascii_lowercase + string.ascii_uppercase + string.digits
			secret_key = "".join(choice(chars) for _ in xrange(32))
			s.set(["server", "secretKey"], secret_key)
			s.save()
		app.secret_key = secret_key
		loginManager = LoginManager()
		loginManager.session_protection = "strong"
		loginManager.user_callback = load_user
		if userManager is None:
			loginManager.anonymous_user = users.DummyUser
			principals.identity_loaders.appendleft(users.dummy_identity_loader)
		loginManager.init_app(app)

		if self._host is None:
			self._host = s.get(["server", "host"])
		if self._port is None:
			self._port = s.getInt(["server", "port"])

		app.debug = self._debug

		# register API blueprint
		self._setup_blueprints()
		def blueprint_enabled(name, plugin):
			if plugin.implementation is None or not isinstance(plugin.implementation, octoprint.plugin.BlueprintPlugin):
				return
			self._register_blueprint_plugin(plugin.implementation)
		pluginLifecycleManager.add_callback(["enabled"], blueprint_enabled)

		## Tornado initialization starts here

		ioloop = IOLoop()
		ioloop.install()

		self._router = SockJSRouter(self._create_socket_connection, "/sockjs")

		upload_suffixes = dict(name=s.get(["server", "uploads", "nameSuffix"]), path=s.get(["server", "uploads", "pathSuffix"]))

		server_routes = self._router.urls + [
			(r"/downloads/timelapse/([^/]*\.mpg)", util.tornado.LargeResponseHandler, dict(path=s.getBaseFolder("timelapse"), as_attachment=True)),
			(r"/downloads/files/local/(.*)", util.tornado.LargeResponseHandler, dict(path=s.getBaseFolder("uploads"), as_attachment=True, path_validation=util.tornado.path_validation_factory(lambda path: not os.path.basename(path).startswith("."), status_code=404))),
			(r"/downloads/logs/([^/]*)", util.tornado.LargeResponseHandler, dict(path=s.getBaseFolder("logs"), as_attachment=True, access_validation=util.tornado.access_validation_factory(app, loginManager, util.flask.admin_validator))),
			(r"/downloads/camera/current", util.tornado.UrlForwardHandler, dict(url=s.get(["webcam", "snapshot"]), as_attachment=True, access_validation=util.tornado.access_validation_factory(app, loginManager, util.flask.user_validator))),
		]
		for name, hook in pluginManager.get_hooks("octoprint.server.http.routes").items():
			try:
				result = hook(list(server_routes))
			except:
				self._logger.exception("There was an error while retrieving additional server routes from plugin hook {name}".format(**locals()))
			else:
				if isinstance(result, (list, tuple)):
					for entry in result:
						if not isinstance(entry, tuple) or not len(entry) == 3:
							continue
						if not isinstance(entry[0], basestring):
							continue
						if not isinstance(entry[2], dict):
							continue

						route, handler, kwargs = entry
						route = r"/plugin/{name}/{route}".format(name=name, route=route if not route.startswith("/") else route[1:])

						self._logger.debug("Adding additional route {route} handled by handler {handler} and with additional arguments {kwargs!r}".format(**locals()))
						server_routes.append((route, handler, kwargs))

		server_routes.append((r".*", util.tornado.UploadStorageFallbackHandler, dict(fallback=util.tornado.WsgiInputContainer(app.wsgi_app), file_prefix="octoprint-file-upload-", file_suffix=".tmp", suffixes=upload_suffixes)))

		self._tornado_app = Application(server_routes)
		max_body_sizes = [
			("POST", r"/api/files/([^/]*)", s.getInt(["server", "uploads", "maxSize"])),
			("POST", r"/api/languages", 5 * 1024 * 1024)
		]

		# allow plugins to extend allowed maximum body sizes
		for name, hook in pluginManager.get_hooks("octoprint.server.http.bodysize").items():
			try:
				result = hook(list(max_body_sizes))
			except:
				self._logger.exception("There was an error while retrieving additional upload sizes from plugin hook {name}".format(**locals()))
			else:
				if isinstance(result, (list, tuple)):
					for entry in result:
						if not isinstance(entry, tuple) or not len(entry) == 3:
							continue
						if not entry[0] in util.tornado.UploadStorageFallbackHandler.BODY_METHODS:
							continue
						if not isinstance(entry[2], int):
							continue

						method, route, size = entry
						route = r"/plugin/{name}/{route}".format(name=name, route=route if not route.startswith("/") else route[1:])

						self._logger.debug("Adding maximum body size of {size}B for {method} requests to {route})".format(**locals()))
						max_body_sizes.append((method, route, size))

		self._server = util.tornado.CustomHTTPServer(self._tornado_app, max_body_sizes=max_body_sizes, default_max_body_size=s.getInt(["server", "maxSize"]))
		self._server.listen(self._port, address=self._host)

		eventManager.fire(events.Events.STARTUP)
		if s.getBoolean(["serial", "autoconnect"]):
			(port, baudrate) = s.get(["serial", "port"]), s.getInt(["serial", "baudrate"])
			printer_profile = printerProfileManager.get_default()
			connectionOptions = get_connection_options()
			if port in connectionOptions["ports"]:
				printer.connect(port=port, baudrate=baudrate, profile=printer_profile["id"] if "id" in printer_profile else "_default")

		# start up watchdogs
		observer = Observer()
		observer.schedule(util.watchdog.GcodeWatchdogHandler(fileManager, printer), s.getBaseFolder("watched"))
		observer.start()

		# run our startup plugins
		octoprint.plugin.call_plugin(octoprint.plugin.StartupPlugin,
		                             "on_startup",
		                             args=(self._host, self._port))

		def call_on_startup(name, plugin):
			implementation = plugin.get_implementation(octoprint.plugin.StartupPlugin)
			if implementation is None:
				return
			implementation.on_startup(self._host, self._port)
		pluginLifecycleManager.add_callback("enabled", call_on_startup)

		# prepare our after startup function
		def on_after_startup():
			self._logger.info("Listening on http://%s:%d" % (self._host, self._port))

			# now this is somewhat ugly, but the issue is the following: startup plugins might want to do things for
			# which they need the server to be already alive (e.g. for being able to resolve urls, such as favicons
			# or service xmls or the like). While they are working though the ioloop would block. Therefore we'll
			# create a single use thread in which to perform our after-startup-tasks, start that and hand back
			# control to the ioloop
			def work():
				octoprint.plugin.call_plugin(octoprint.plugin.StartupPlugin,
				                             "on_after_startup")

				def call_on_after_startup(name, plugin):
					implementation = plugin.get_implementation(octoprint.plugin.StartupPlugin)
					if implementation is None:
						return
					implementation.on_after_startup()
				pluginLifecycleManager.add_callback("enabled", call_on_after_startup)

			import threading
			threading.Thread(target=work).start()
		ioloop.add_callback(on_after_startup)

		# prepare our shutdown function
		def on_shutdown():
			self._logger.info("Goodbye!")
			observer.stop()
			observer.join()
			octoprint.plugin.call_plugin(octoprint.plugin.ShutdownPlugin,
			                             "on_shutdown")
		atexit.register(on_shutdown)

		try:
			ioloop.start()
		except KeyboardInterrupt:
			pass
		except:
			self._logger.fatal("Now that is embarrassing... Something really really went wrong here. Please report this including the stacktrace below in OctoPrint's bugtracker. Thanks!")
			self._logger.exception("Stacktrace follows:")

	def _create_socket_connection(self, session):
		global printer, fileManager, analysisQueue, userManager, eventManager
		return util.sockjs.PrinterStateConnection(printer, fileManager, analysisQueue, userManager, eventManager, pluginManager, session)

	def _check_for_root(self):
		if "geteuid" in dir(os) and os.geteuid() == 0:
			exit("You should not run OctoPrint as root!")

	def _get_locale(self):
		global LANGUAGES

		if "l10n" in request.values:
			return Locale.negotiate([request.values["l10n"]], LANGUAGES)

		if hasattr(g, "identity") and g.identity and userManager is not None:
			userid = g.identity.id
			try:
				user_language = userManager.getUserSetting(userid, ("interface", "language"))
				if user_language is not None and not user_language == "_default":
					return Locale.negotiate([user_language], LANGUAGES)
			except octoprint.users.UnknownUser:
				pass

		default_language = settings().get(["appearance", "defaultLanguage"])
		if default_language is not None and not default_language == "_default" and default_language in LANGUAGES:
			return Locale.negotiate([default_language], LANGUAGES)

		return request.accept_languages.best_match(LANGUAGES)

	def _setup_logging(self, debug, logConf=None):
		defaultConfig = {
			"version": 1,
			"formatters": {
				"simple": {
					"format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
				}
			},
			"handlers": {
				"console": {
					"class": "logging.StreamHandler",
					"level": "DEBUG",
					"formatter": "simple",
					"stream": "ext://sys.stdout"
				},
				"file": {
					"class": "logging.handlers.TimedRotatingFileHandler",
					"level": "DEBUG",
					"formatter": "simple",
					"when": "D",
					"backupCount": "1",
					"filename": os.path.join(settings().getBaseFolder("logs"), "octoprint.log")
				},
				"serialFile": {
					"class": "logging.handlers.RotatingFileHandler",
					"level": "DEBUG",
					"formatter": "simple",
					"maxBytes": 2 * 1024 * 1024, # let's limit the serial log to 2MB in size
					"filename": os.path.join(settings().getBaseFolder("logs"), "serial.log")
				}
			},
			"loggers": {
				"SERIAL": {
					"level": "CRITICAL",
					"handlers": ["serialFile"],
					"propagate": False
				},
				"tornado.application": {
					"level": "INFO"
				},
				"tornado.general": {
					"level": "INFO"
				}
			},
			"root": {
				"level": "INFO",
				"handlers": ["console", "file"]
			}
		}

		if debug:
			defaultConfig["root"]["level"] = "DEBUG"

		if logConf is None:
			logConf = os.path.join(settings().getBaseFolder("base"), "logging.yaml")

		configFromFile = {}
		if os.path.exists(logConf) and os.path.isfile(logConf):
			import yaml
			with open(logConf, "r") as f:
				configFromFile = yaml.safe_load(f)

		config = octoprint.util.dict_merge(defaultConfig, configFromFile)
		logging.config.dictConfig(config)
		logging.captureWarnings(True)

		import warnings
		warnings.simplefilter("always")

		if settings().getBoolean(["serial", "log"]):
			# enable debug logging to serial.log
			logging.getLogger("SERIAL").setLevel(logging.DEBUG)
			logging.getLogger("SERIAL").debug("Enabling serial logging")

	def _setup_app(self):
		@app.before_request
		def before_request():
			g.locale = self._get_locale()

		@app.after_request
		def after_request(response):
			# send no-cache headers with all POST responses
			if request.method == "POST":
				response.cache_control.no_cache = True
			response.headers.add("X-Clacks-Overhead", "GNU Terry Pratchett")
			return response

	def _setup_i18n(self, app):
		global babel
		global LOCALES
		global LANGUAGES

		babel = Babel(app)

		def get_available_locale_identifiers(locales):
			result = set()

			# add available translations
			for locale in locales:
				result.add(locale.language)
				if locale.territory:
					# if a territory is specified, add that too
					result.add("%s_%s" % (locale.language, locale.territory))

			return result

		LOCALES = babel.list_translations()
		LANGUAGES = get_available_locale_identifiers(LOCALES)

		@babel.localeselector
		def get_locale():
			return self._get_locale()

	def _setup_jinja2(self):
		app.jinja_env.add_extension("jinja2.ext.do")

		# configure additional template folders for jinja2
		import jinja2
		filesystem_loader = jinja2.FileSystemLoader([])
		filesystem_loader.searchpath = self._template_searchpaths

		jinja_loader = jinja2.ChoiceLoader([
			app.jinja_loader,
			filesystem_loader
		])
		app.jinja_loader = jinja_loader
		del jinja2

		self._register_template_plugins()

	def _register_template_plugins(self):
		template_plugins = pluginManager.get_implementations(octoprint.plugin.TemplatePlugin)
		for plugin in template_plugins:
			self._register_additional_template_plugin(plugin)

	def _register_additional_template_plugin(self, plugin):
		folder = plugin.get_template_folder()
		if folder is not None and not folder in self._template_searchpaths:
			self._template_searchpaths.append(folder)

	def _unregister_additional_template_plugin(self, plugin):
		folder = plugin.get_template_folder()
		if folder is not None and folder in self._template_searchpaths:
			self._template_searchpaths.remove(folder)

	def _setup_blueprints(self):
		from octoprint.server.api import api
		from octoprint.server.apps import apps
		import octoprint.server.views

		app.register_blueprint(api, url_prefix="/api")
		app.register_blueprint(apps, url_prefix="/apps")

		# also register any blueprints defined in BlueprintPlugins
		self._register_blueprint_plugins()

	def _register_blueprint_plugins(self):
		blueprint_plugins = octoprint.plugin.plugin_manager().get_implementations(octoprint.plugin.BlueprintPlugin)
		for plugin in blueprint_plugins:
			self._register_blueprint_plugin(plugin)

	def _register_blueprint_plugin(self, plugin):
		name = plugin._identifier
		blueprint = plugin.get_blueprint()
		if blueprint is None:
			return

		if plugin.is_blueprint_protected():
			from octoprint.server.util import apiKeyRequestHandler, corsResponseHandler
			blueprint.before_request(apiKeyRequestHandler)
			blueprint.after_request(corsResponseHandler)

		url_prefix = "/plugin/{name}".format(name=name)
		app.register_blueprint(blueprint, url_prefix=url_prefix)

		if self._logger:
			self._logger.debug("Registered API of plugin {name} under URL prefix {url_prefix}".format(name=name, url_prefix=url_prefix))

class LifecycleManager(object):
	def __init__(self, plugin_manager):
		self._plugin_manager = plugin_manager

		self._plugin_lifecycle_callbacks = defaultdict(list)
		self._logger = logging.getLogger(__name__)

		def on_plugin_event_factory(lifecycle_event):
			def on_plugin_event(name, plugin):
				self.on_plugin_event(lifecycle_event, name, plugin)
			return on_plugin_event

		self._plugin_manager.on_plugin_loaded = on_plugin_event_factory("loaded")
		self._plugin_manager.on_plugin_unloaded = on_plugin_event_factory("unloaded")
		self._plugin_manager.on_plugin_activated = on_plugin_event_factory("activated")
		self._plugin_manager.on_plugin_deactivated = on_plugin_event_factory("deactivated")
		self._plugin_manager.on_plugin_enabled = on_plugin_event_factory("enabled")
		self._plugin_manager.on_plugin_disabled = on_plugin_event_factory("disabled")

	def on_plugin_event(self, event, name, plugin):
		for lifecycle_callback in self._plugin_lifecycle_callbacks[event]:
			lifecycle_callback(name, plugin)

	def add_callback(self, events, callback):
		if isinstance(events, (str, unicode)):
			events = [events]

		for event in events:
			self._plugin_lifecycle_callbacks[event].append(callback)

	def remove_callback(self, callback, events=None):
		if events is None:
			for event in self._plugin_lifecycle_callbacks:
				if callback in self._plugin_lifecycle_callbacks[event]:
					self._plugin_lifecycle_callbacks[event].remove(callback)
		else:
			if isinstance(events, (str, unicode)):
				events = [events]

			for event in events:
				if callback in self._plugin_lifecycle_callbacks[event]:
					self._plugin_lifecycle_callbacks[event].remove(callback)

if __name__ == "__main__":
	server = Server()
	server.run()