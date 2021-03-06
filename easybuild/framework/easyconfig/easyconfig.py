# #
# Copyright 2009-2018 Ghent University
#
# This file is part of EasyBuild,
# originally created by the HPC team of Ghent University (http://ugent.be/hpc/en),
# with support of Ghent University (http://ugent.be/hpc),
# the Flemish Supercomputer Centre (VSC) (https://www.vscentrum.be),
# Flemish Research Foundation (FWO) (http://www.fwo.be/en)
# and the Department of Economy, Science and Innovation (EWI) (http://www.ewi-vlaanderen.be/en).
#
# https://github.com/easybuilders/easybuild
#
# EasyBuild is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation v2.
#
# EasyBuild is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with EasyBuild.  If not, see <http://www.gnu.org/licenses/>.
# #

"""
Easyconfig module that contains the EasyConfig class.

:author: Stijn De Weirdt (Ghent University)
:author: Dries Verdegem (Ghent University)
:author: Kenneth Hoste (Ghent University)
:author: Pieter De Baets (Ghent University)
:author: Jens Timmerman (Ghent University)
:author: Toon Willems (Ghent University)
:author: Ward Poelmans (Ghent University)
:author: Alan O'Cais (Juelich Supercomputing Centre)
:author: Bart Oldeman (McGill University, Calcul Quebec, Compute Canada)
:author: Maxime Boissonneault (Universite Laval, Calcul Quebec, Compute Canada)
"""

import copy
import difflib
import functools
import os
import re
from vsc.utils import fancylogger
from vsc.utils.missing import get_class_for, nub
from vsc.utils.patterns import Singleton
from distutils.version import LooseVersion

from easybuild.framework.easyconfig import MANDATORY
from easybuild.framework.easyconfig.constants import EXTERNAL_MODULE_MARKER
from easybuild.framework.easyconfig.default import DEFAULT_CONFIG
from easybuild.framework.easyconfig.format.convert import Dependency
from easybuild.framework.easyconfig.format.format import DEPENDENCY_PARAMETERS
from easybuild.framework.easyconfig.format.one import EB_FORMAT_EXTENSION, retrieve_blocks_in_spec
from easybuild.framework.easyconfig.format.yeb import YEB_FORMAT_EXTENSION, is_yeb_format
from easybuild.framework.easyconfig.licenses import EASYCONFIG_LICENSES_DICT
from easybuild.framework.easyconfig.parser import DEPRECATED_PARAMETERS, REPLACED_PARAMETERS
from easybuild.framework.easyconfig.parser import EasyConfigParser, fetch_parameters_from_easyconfig
from easybuild.framework.easyconfig.templates import TEMPLATE_CONSTANTS, template_constant_dict
from easybuild.tools.build_log import EasyBuildError, print_warning
from easybuild.tools.config import build_option, get_module_naming_scheme
from easybuild.tools.filetools import EASYBLOCK_CLASS_PREFIX
from easybuild.tools.filetools import copy_file, decode_class_name, encode_class_name, read_file, write_file
from easybuild.tools.hooks import PARSE, load_hooks, run_hook
from easybuild.tools.module_naming_scheme import DEVEL_MODULE_SUFFIX
from easybuild.tools.module_naming_scheme.utilities import avail_module_naming_schemes, det_full_ec_version
from easybuild.tools.module_naming_scheme.utilities import det_hidden_modname, is_valid_module_name
from easybuild.tools.modules import modules_tool
from easybuild.tools.ordereddict import OrderedDict
from easybuild.tools.systemtools import check_os_dependency
from easybuild.tools.toolchain import DUMMY_TOOLCHAIN_NAME, DUMMY_TOOLCHAIN_VERSION
from easybuild.tools.toolchain.toolchain import TOOLCHAIN_CAPABILITIES, TOOLCHAIN_CAPABILITY_CUDA
from easybuild.tools.toolchain.utilities import get_toolchain, search_toolchain
from easybuild.tools.utilities import quote_py_str, remove_unwanted_chars
from easybuild.tools.version import VERSION
from easybuild.toolchains.compiler.cuda import Cuda

_log = fancylogger.getLogger('easyconfig.easyconfig', fname=False)

# add license here to make it really MANDATORY (remove comment in default)
MANDATORY_PARAMS = ['name', 'version', 'homepage', 'description', 'toolchain']

# set of configure/build/install options that can be provided as lists for an iterated build
ITERATE_OPTIONS = ['preconfigopts', 'configopts', 'prebuildopts', 'buildopts', 'preinstallopts', 'installopts']

# name of easyconfigs archive subdirectory
EASYCONFIGS_ARCHIVE_DIR = '__archive__'


try:
    import autopep8
    HAVE_AUTOPEP8 = True
except ImportError as err:
    _log.warning("Failed to import autopep8, dumping easyconfigs with reformatting enabled will not work: %s", err)
    HAVE_AUTOPEP8 = False


_easyconfig_files_cache = {}
_easyconfigs_cache = {}


def handle_deprecated_or_replaced_easyconfig_parameters(ec_method):
    """Decorator to handle deprecated/replaced easyconfig parameters."""
    def new_ec_method(self, key, *args, **kwargs):
        """Check whether any replace easyconfig parameters are still used"""
        # map deprecated parameters to their replacements, issue deprecation warning(/error)
        if key in DEPRECATED_PARAMETERS:
            depr_key = key
            key, ver = DEPRECATED_PARAMETERS[depr_key]
            _log.deprecated("Easyconfig parameter '%s' is deprecated, use '%s' instead." % (depr_key, key), ver)
        if key in REPLACED_PARAMETERS:
            _log.nosupport("Easyconfig parameter '%s' is replaced by '%s'" % (key, REPLACED_PARAMETERS[key]), '2.0')
        return ec_method(self, key, *args, **kwargs)

    return new_ec_method


def toolchain_hierarchy_cache(func):
    """Function decorator to cache (and retrieve cached) toolchain hierarchy queries."""
    cache = {}

    @functools.wraps(func)
    def cache_aware_func(toolchain, incl_capabilities=False):
        """Look up toolchain hierarchy in cache first, determine and cache it if not available yet."""
        cache_key = (toolchain['name'], toolchain['version'], incl_capabilities)

        # fetch from cache if available, cache it if it's not
        if cache_key in cache:
            _log.debug("Using cache to return hierarchy for toolchain %s: %s", str(toolchain), cache[cache_key])
            return cache[cache_key]
        else:
            toolchain_hierarchy = func(toolchain, incl_capabilities)
            cache[cache_key] = toolchain_hierarchy
            return cache[cache_key]

    # Expose clear method of cache to wrapped function
    cache_aware_func.clear = cache.clear

    return cache_aware_func


def det_subtoolchain_version(current_tc, subtoolchain_name, optional_toolchains, cands, incl_capabilities=False):
    """
    Returns unique version for subtoolchain, in tc dict.
    If there is no unique version:
    * use '' for dummy, if dummy is not skipped.
    * return None for skipped subtoolchains, that is,
      optional toolchains or dummy without add_dummy_to_minimal_toolchains.
    * in all other cases, raises an exception.
    """
    uniq_subtc_versions = set([subtc['version'] for subtc in cands if subtc['name'] == subtoolchain_name])
    # init with "skipped"
    subtoolchain_version = None

    # dummy toolchain: bottom of the hierarchy
    if subtoolchain_name == DUMMY_TOOLCHAIN_NAME:
        if build_option('add_dummy_to_minimal_toolchains') and not incl_capabilities:
            subtoolchain_version = ''
    elif len(uniq_subtc_versions) == 1:
        subtoolchain_version = list(uniq_subtc_versions)[0]
    elif len(uniq_subtc_versions) == 0:
        if subtoolchain_name not in optional_toolchains:
            # raise error if the subtoolchain considered now is not optional
            raise EasyBuildError("No version found for subtoolchain %s in dependencies of %s",
                                 subtoolchain_name, current_tc['name'])
    else:
        raise EasyBuildError("Multiple versions of %s found in dependencies of toolchain %s: %s",
                             subtoolchain_name, current_tc['name'], ', '.join(sorted(uniq_subtc_versions)))

    return subtoolchain_version


@toolchain_hierarchy_cache
def get_toolchain_hierarchy(parent_toolchain, incl_capabilities=False):
    """
    Determine list of subtoolchains for specified parent toolchain.
    Result starts with the most minimal subtoolchains first, ends with specified toolchain.

    The dummy toolchain is considered the most minimal subtoolchain only if the add_dummy_to_minimal_toolchains
    build option is enabled.

    The most complex hierarchy we have now is goolfc which works as follows:

        goolfc
        /     \
     gompic    golfc(*)
          \    /   \      (*) optional toolchains, not compulsory for backwards compatibility
          gcccuda golf(*)
              \   /
               GCC
              /  |
      GCCcore(*) |
              \  |
             (dummy: only considered if --add-dummy-to-minimal-toolchains configuration option is enabled)

    :param parent_toolchain: dictionary with name/version of parent toolchain
    :param incl_capabilities: also register toolchain capabilities in result
    """
    # obtain list of all possible subtoolchains
    _, all_tc_classes = search_toolchain('')
    subtoolchains = dict((tc_class.NAME, getattr(tc_class, 'SUBTOOLCHAIN', None)) for tc_class in all_tc_classes)
    optional_toolchains = set(tc_class.NAME for tc_class in all_tc_classes if getattr(tc_class, 'OPTIONAL', False))
    composite_toolchains = set(tc_class.NAME for tc_class in all_tc_classes if len(tc_class.__bases__) > 1)

    # the parent toolchain is at the top of the hierarchy,
    # we need a copy so that adding capabilities (below) doesn't affect the original object
    toolchain_hierarchy = [copy.copy(parent_toolchain)]
    # use a queue to handle a breadth-first-search of the hierarchy,
    # which is required to take into account the potential for multiple subtoolchains
    bfs_queue = [parent_toolchain]
    visited = set()

    while bfs_queue:
        current_tc = bfs_queue.pop()
        current_tc_name, current_tc_version = current_tc['name'], current_tc['version']
        subtoolchain_names = subtoolchains[current_tc_name]
        # if current toolchain has no subtoolchains, consider next toolchain in queue
        if subtoolchain_names is None:
            continue
        # make sure we always have a list of subtoolchains, even if there's only one
        if not isinstance(subtoolchain_names, list):
            subtoolchain_names = [subtoolchain_names]
        # grab the easyconfig of the current toolchain and search the dependencies for a version of the subtoolchain
        path = robot_find_easyconfig(current_tc_name, current_tc_version)
        if path is None:
            raise EasyBuildError("Could not find easyconfig for %s toolchain version %s",
                                 current_tc_name, current_tc_version)

        # parse the easyconfig
        parsed_ec = process_easyconfig(path, validate=False)[0]

        # search for version of the subtoolchain in dependencies
        # considers deps + toolchains of deps + deps of deps + toolchains of deps of deps
        # consider both version and versionsuffix for dependencies
        cands = []
        for dep in parsed_ec['ec'].dependencies():
            # skip dependencies that are marked as external modules
            if dep['external_module']:
                continue

            # include dep and toolchain of dep as candidates
            cands.extend([
                {'name': dep['name'], 'version': dep['version'] + dep['versionsuffix']},
                dep['toolchain'],
            ])

            # find easyconfig file for this dep and parse it
            ecfile = robot_find_easyconfig(dep['name'], det_full_ec_version(dep))
            if ecfile is None:
                raise EasyBuildError("Could not find easyconfig for dependency %s with version %s",
                                     dep['name'], det_full_ec_version(dep))
            easyconfig = process_easyconfig(ecfile, validate=False)[0]['ec']

            # include deps and toolchains of deps of this dep, but skip dependencies marked as external modules
            for depdep in easyconfig.dependencies():
                if depdep['external_module']:
                    continue

                cands.append({'name': depdep['name'], 'version': depdep['version'] + depdep['versionsuffix']})
                cands.append(depdep['toolchain'])

        for dep in subtoolchain_names:
            # try to find subtoolchains with the same version as the parent
            # only do this for composite toolchains, not single-compiler toolchains, whose
            # versions match those of the component instead of being e.g. "2018a".
            if dep in composite_toolchains:
                ecfile = robot_find_easyconfig(dep, current_tc_version)
                if ecfile is not None:
                    cands.append({'name': dep, 'version': current_tc_version})

        # only retain candidates that match subtoolchain names
        cands = [c for c in cands if c['name'] in subtoolchain_names]

        for subtoolchain_name in subtoolchain_names:
            subtoolchain_version = det_subtoolchain_version(current_tc, subtoolchain_name, optional_toolchains, cands,
                                                            incl_capabilities=incl_capabilities)
            # add to hierarchy and move to next
            if subtoolchain_version is not None and subtoolchain_name not in visited:
                tc = {'name': subtoolchain_name, 'version': subtoolchain_version}
                toolchain_hierarchy.insert(0, tc)
                bfs_queue.insert(0, tc)
                visited.add(subtoolchain_name)

    # also add toolchain capabilities
    if incl_capabilities:
        for toolchain in toolchain_hierarchy:
            toolchain_class, _ = search_toolchain(toolchain['name'])
            tc = toolchain_class(version=toolchain['version'])
            for capability in TOOLCHAIN_CAPABILITIES:
                # cuda is the special case which doesn't have a family attribute
                if capability == TOOLCHAIN_CAPABILITY_CUDA:
                    # use None rather than False, useful to have it consistent with the rest
                    toolchain[capability] = isinstance(tc, Cuda) or None
                elif hasattr(tc, capability):
                    toolchain[capability] = getattr(tc, capability)()

    _log.info("Found toolchain hierarchy for toolchain %s: %s", parent_toolchain, toolchain_hierarchy)
    return toolchain_hierarchy


class EasyConfig(object):
    """
    Class which handles loading, reading, validation of easyconfigs
    """

    def __init__(self, path, extra_options=None, build_specs=None, validate=True, hidden=None, rawtxt=None,
                 auto_convert_value_types=True):
        """
        initialize an easyconfig.
        :param path: path to easyconfig file to be parsed (ignored if rawtxt is specified)
        :param extra_options: dictionary with extra variables that can be set for this specific instance
        :param build_specs: dictionary of build specifications (see EasyConfig class, default: {})
        :param validate: indicates whether validation should be performed (note: combined with 'validate' build option)
        :param hidden: indicate whether corresponding module file should be installed hidden ('.'-prefixed)
        :param rawtxt: raw contents of easyconfig file
        :param auto_convert_value_types: indicates wether types of easyconfig values should be automatically converted
                                         in case they are wrong
        """
        self.template_values = None
        self.enable_templating = True  # a boolean to control templating

        self.log = fancylogger.getLogger(self.__class__.__name__, fname=False)

        if path is not None and not os.path.isfile(path):
            raise EasyBuildError("EasyConfig __init__ expected a valid path")

        # read easyconfig file contents (or use provided rawtxt), so it can be passed down to avoid multiple re-reads
        self.path = None
        if rawtxt is None:
            self.path = path
            self.rawtxt = read_file(path)
            self.log.debug("Raw contents from supplied easyconfig file %s: %s" % (path, self.rawtxt))
        else:
            self.rawtxt = rawtxt
            self.log.debug("Supplied raw easyconfig contents: %s" % self.rawtxt)

        # constructing easyconfig parser object includes a "raw" parse,
        # which serves as a check to see whether supplied easyconfig file is an actual easyconfig...
        self.log.info("Performing quick parse to check for valid easyconfig file...")
        self.parser = EasyConfigParser(filename=self.path, rawcontent=self.rawtxt,
                                       auto_convert_value_types=auto_convert_value_types)

        self.modules_tool = modules_tool()

        # use legacy module classes as default
        self.valid_module_classes = build_option('valid_module_classes')
        if self.valid_module_classes is not None:
            self.log.info("Obtained list of valid module classes: %s" % self.valid_module_classes)

        self._config = copy.deepcopy(DEFAULT_CONFIG)

        # obtain name and easyblock specifications from raw easyconfig contents
        self.software_name, self.easyblock = fetch_parameters_from_easyconfig(self.rawtxt, ['name', 'easyblock'])

        # determine line of extra easyconfig parameters
        if extra_options is None:
            easyblock_class = get_easyblock_class(self.easyblock, name=self.software_name)
            self.extra_options = easyblock_class.extra_options()
        else:
            self.extra_options = extra_options

        if not isinstance(self.extra_options, dict):
            tup = (type(self.extra_options), self.extra_options)
            self.log.nosupport("extra_options return value should be of type 'dict', found '%s': %s" % tup, '2.0')

        self.mandatory = MANDATORY_PARAMS[:]

        # deep copy to make sure self.extra_options remains unchanged
        self.extend_params(copy.deepcopy(self.extra_options))

        # set valid stops
        self.valid_stops = build_option('valid_stops')
        self.log.debug("List of valid stops obtained: %s" % self.valid_stops)

        # store toolchain
        self._toolchain = None

        self.validations = {
            'moduleclass': self.valid_module_classes,
            'stop': self.valid_stops,
        }

        self.external_modules_metadata = build_option('external_modules_metadata')

        # parse easyconfig file
        self.build_specs = build_specs
        self.parse()

        # check whether this easyconfig file is deprecated, and act accordingly if so
        self.check_deprecated(self.path)

        # perform validations
        self.validation = build_option('validate') and validate
        if self.validation:
            self.validate(check_osdeps=build_option('check_osdeps'))

        # filter hidden dependencies from list of dependencies
        self.filter_hidden_deps()

        self._all_dependencies = None

        # keep track of whether the generated module file should be hidden
        if hidden is None:
            hidden = self['hidden'] or build_option('hidden')
        self.hidden = hidden

        # set installdir/module info
        mns = ActiveMNS()
        self.full_mod_name = mns.det_full_module_name(self)
        self.short_mod_name = mns.det_short_module_name(self)
        self.mod_subdir = mns.det_module_subdir(self)

        self.software_license = None

    def filename(self):
        """Determine correct filename for this easyconfig file."""

        if is_yeb_format(self.path, self.rawtxt):
            ext = YEB_FORMAT_EXTENSION
        else:
            ext = EB_FORMAT_EXTENSION

        return '%s-%s%s' % (self.name, det_full_ec_version(self), ext)

    def extend_params(self, extra, overwrite=True):
        """Extend list of known parameters via provided list of extra easyconfig parameters."""

        self.log.debug("Extending list of known easyconfig parameters with: %s", ' '.join(extra.keys()))

        if overwrite:
            self._config.update(extra)
        else:
            for key in extra:
                if key not in self._config:
                    self._config[key] = extra[key]
                    self.log.debug("Added new easyconfig parameter: %s", key)
                else:
                    self.log.debug("Easyconfig parameter %s already known, not overwriting", key)

        # extend mandatory keys
        for key, value in extra.items():
            if value[2] == MANDATORY:
                self.mandatory.append(key)
        self.log.debug("Updated list of mandatory easyconfig parameters: %s", self.mandatory)

    def copy(self):
        """
        Return a copy of this EasyConfig instance.
        """
        # create a new EasyConfig instance
        ec = EasyConfig(self.path, validate=self.validation, hidden=self.hidden, rawtxt=self.rawtxt)
        # take a copy of the actual config dictionary (which already contains the extra options)
        ec._config = copy.deepcopy(self._config)
        # since rawtxt is defined, self.path may not get inherited, make sure it does
        if self.path:
            ec.path = self.path

        return ec

    def update(self, key, value, allow_duplicate=True):
        """
        Update a string configuration value with a value (i.e. append to it).
        """
        prev_value = self[key]
        if isinstance(prev_value, basestring):
            if allow_duplicate or value not in prev_value:
                self[key] = '%s %s ' % (prev_value, value)
        elif isinstance(prev_value, list):
            if allow_duplicate:
                self[key] = prev_value + value
            else:
                for item in value:
                    # add only those items that aren't already in the list
                    if item not in prev_value:
                        self[key] = prev_value + [item]
        else:
            raise EasyBuildError("Can't update configuration value for %s, because it's not a string or list.", key)

    def parse(self):
        """
        Parse the file and set options
        mandatory requirements are checked here
        """
        if self.build_specs is None:
            arg_specs = {}
        elif isinstance(self.build_specs, dict):
            # build a new dictionary with only the expected keys, to pass as named arguments to get_config_dict()
            arg_specs = self.build_specs
        else:
            raise EasyBuildError("Specifications should be specified using a dictionary, got %s",
                                 type(self.build_specs))
        self.log.debug("Obtained specs dict %s" % arg_specs)

        self.log.info("Parsing easyconfig file %s with rawcontent: %s" % (self.path, self.rawtxt))
        self.parser.set_specifications(arg_specs)
        local_vars = self.parser.get_config_dict()
        self.log.debug("Parsed easyconfig as a dictionary: %s" % local_vars)

        # make sure all mandatory parameters are defined
        # this includes both generic mandatory parameters and software-specific parameters defined via extra_options
        missing_mandatory_keys = [key for key in self.mandatory if key not in local_vars]
        if missing_mandatory_keys:
            raise EasyBuildError("mandatory parameters not provided in %s: %s", self.path, missing_mandatory_keys)

        # provide suggestions for typos
        possible_typos = [(key, difflib.get_close_matches(key.lower(), self._config.keys(), 1, 0.85))
                          for key in local_vars if key not in self]

        typos = [(key, guesses[0]) for (key, guesses) in possible_typos if len(guesses) == 1]
        if typos:
            raise EasyBuildError("You may have some typos in your easyconfig file: %s",
                                 ', '.join(["%s -> %s" % typo for typo in typos]))

        # we need toolchain to be set when we call _parse_dependency
        for key in ['toolchain'] + local_vars.keys():
            # validations are skipped, just set in the config
            if key in self._config.keys():
                self[key] = local_vars[key]
                self.log.info("setting config option %s: value %s (type: %s)", key, self[key], type(self[key]))
            elif key in REPLACED_PARAMETERS:
                _log.nosupport("Easyconfig parameter '%s' is replaced by '%s'" % (key, REPLACED_PARAMETERS[key]), '2.0')

            # do not store variables we don't need
            else:
                self.log.debug("Ignoring unknown easyconfig parameter %s (value: %s)" % (key, local_vars[key]))

        # trigger parse hook
        # templating is disabled when parse_hook is called to allow for easy updating of mutable easyconfig parameters
        # (see also comment in resolve_template)
        hooks = load_hooks(build_option('hooks'))
        prev_enable_templating = self.enable_templating
        self.enable_templating = False

        parse_hook_msg = None
        if self.path:
            parse_hook_msg = "Running %s hook for %s..." % (PARSE, os.path.basename(self.path))

        run_hook(PARSE, hooks, args=[self], msg=parse_hook_msg)

        # parse dependency specifications
        # it's important that templating is still disabled at this stage!
        self.log.info("Parsing dependency specifications...")
        self['builddependencies'] = [self._parse_dependency(dep, build_only=True) for dep in self['builddependencies']]
        self['dependencies'] = [self._parse_dependency(dep) for dep in self['dependencies']]
        self['hiddendependencies'] = [self._parse_dependency(dep, hidden=True) for dep in self['hiddendependencies']]

        # restore templating
        self.enable_templating = prev_enable_templating

        # update templating dictionary
        self.generate_template_values()

        # finalize dependencies w.r.t. minimal toolchains & module names
        self._finalize_dependencies()

        # indicate that this is a parsed easyconfig
        self._config['parsed'] = [True, "This is a parsed easyconfig", "HIDDEN"]

    def check_deprecated(self, path):
        """Check whether this easyconfig file is deprecated."""

        depr_msgs = []

        deprecated = self['deprecated']
        if deprecated:
            if isinstance(deprecated, basestring):
                depr_msgs.append("easyconfig file '%s' is marked as deprecated:\n%s\n" % (path, deprecated))
            else:
                raise EasyBuildError("Wrong type for value of 'deprecated' easyconfig parameter: %s", type(deprecated))

        if self.toolchain.is_deprecated():
            depr_msgs.append("toolchain '%(name)s/%(version)s' is marked as deprecated" % self['toolchain'])

        if depr_msgs:
            depr_maj_ver = int(str(VERSION).split('.')[0]) + 1
            more_info_depr_ec = " (see also http://easybuild.readthedocs.org/en/latest/Deprecated-easyconfigs.html)"
            self.log.deprecated(', '.join(depr_msgs), '%s.0' % depr_maj_ver, more_info=more_info_depr_ec)

    def validate(self, check_osdeps=True):
        """
        Validate this easyonfig
        - ensure certain easyconfig parameters are set to a known value (see self.validations)
        - check OS dependencies
        - check license
        """
        self.log.info("Validating easyconfig")
        for attr in self.validations:
            self._validate(attr, self.validations[attr])

        if check_osdeps:
            self.log.info("Checking OS dependencies")
            self.validate_os_deps()
        else:
            self.log.info("Not checking OS dependencies")

        self.log.info("Checking skipsteps")
        if not isinstance(self._config['skipsteps'][0], (list, tuple,)):
            raise EasyBuildError('Invalid type for skipsteps. Allowed are list or tuple, got %s (%s)',
                                 type(self._config['skipsteps'][0]), self._config['skipsteps'][0])

        self.log.info("Checking build option lists")
        self.validate_iterate_opts_lists()

        self.log.info("Checking licenses")
        self.validate_license()

    def validate_license(self):
        """Validate the license"""
        lic = self['software_license']
        if lic is None:
            # when mandatory, remove this possibility
            if 'software_license' in self.mandatory:
                raise EasyBuildError("Software license is mandatory, but 'software_license' is undefined")
        elif lic in EASYCONFIG_LICENSES_DICT:
            # create License instance
            self.software_license = EASYCONFIG_LICENSES_DICT[lic]()
        else:
            known_licenses = ', '.join(sorted(EASYCONFIG_LICENSES_DICT.keys()))
            raise EasyBuildError("Invalid license %s (known licenses: %s)", lic, known_licenses)

        # TODO, when GROUP_SOURCE and/or GROUP_BINARY is True
        #  check the owner of source / binary (must match 'group' parameter from easyconfig)

        return True

    def validate_os_deps(self):
        """
        validate presence of OS dependencies
        osdependencies should be a single list
        """
        not_found = []
        for dep in self['osdependencies']:
            # make sure we have a tuple
            if isinstance(dep, basestring):
                dep = (dep,)
            elif not isinstance(dep, tuple):
                raise EasyBuildError("Non-tuple value type for OS dependency specification: %s (type %s)",
                                     dep, type(dep))

            if not any([check_os_dependency(cand_dep) for cand_dep in dep]):
                not_found.append(dep)

        if not_found:
            raise EasyBuildError("One or more OS dependencies were not found: %s", not_found)
        else:
            self.log.info("OS dependencies ok: %s" % self['osdependencies'])

        return True

    def validate_iterate_opts_lists(self):
        """
        Configure/build/install options specified as lists should have same length.
        """

        # configure/build/install options may be lists, in case of an iterated build
        # when lists are used, they should be all of same length
        # list of length 1 are treated as if it were strings in EasyBlock
        opt_counts = []
        for opt in ITERATE_OPTIONS:

            # anticipate changes in available easyconfig parameters (e.g. makeopts -> buildopts?)
            if self.get(opt, None) is None:
                raise EasyBuildError("%s not available in self.cfg (anymore)?!", opt)

            # keep track of list, supply first element as first option to handle
            if isinstance(self[opt], (list, tuple)):
                opt_counts.append((opt, len(self[opt])))

        # make sure that options that specify lists have the same length
        list_opt_lengths = [length for (opt, length) in opt_counts if length > 1]
        if len(nub(list_opt_lengths)) > 1:
            raise EasyBuildError("Build option lists for iterated build should have same length: %s", opt_counts)

        return True

    def filter_hidden_deps(self):
        """
        Filter hidden dependencies from list of (build) dependencies.
        """
        dep_mod_names = [dep['full_mod_name'] for dep in self['dependencies'] + self['builddependencies']]
        build_dep_mod_names = [dep['full_mod_name'] for dep in self['builddependencies']]

        faulty_deps = []
        for i, hidden_dep in enumerate(self['hiddendependencies']):
            hidden_mod_name = ActiveMNS().det_full_module_name(hidden_dep)
            visible_mod_name = ActiveMNS().det_full_module_name(hidden_dep, force_visible=True)

            # track whether this hidden dep is listed as a build dep
            if visible_mod_name in build_dep_mod_names or hidden_mod_name in build_dep_mod_names:
                # templating must be temporarily disabled when updating a value in a dict;
                # see comments in resolve_template
                enable_templating = self.enable_templating
                self.enable_templating = False
                self['hiddendependencies'][i]['build_only'] = True
                self.enable_templating = enable_templating

            # filter hidden dep from list of (build)dependencies
            if visible_mod_name in dep_mod_names:
                for key in ['builddependencies', 'dependencies']:
                    self[key] = [d for d in self[key] if d['full_mod_name'] != visible_mod_name]
                self.log.debug("Removed (build)dependency matching hidden dependency %s", hidden_dep)
            elif hidden_mod_name in dep_mod_names:
                for key in ['builddependencies', 'dependencies']:
                    self[key] = [d for d in self[key] if d['full_mod_name'] != hidden_mod_name]
                self.log.debug("Hidden (build)dependency %s is already marked to be installed as a hidden module",
                               hidden_dep)
            else:
                # hidden dependencies must also be included in list of dependencies;
                # this is done to try and make easyconfigs portable w.r.t. site-specific policies with minimal effort,
                # i.e. by simply removing the 'hiddendependencies' specification
                self.log.warning("Hidden dependency %s not in list of (build)dependencies", visible_mod_name)
                faulty_deps.append(visible_mod_name)

        if faulty_deps:
            raise EasyBuildError("Hidden deps with visible module names %s not in list of (build)dependencies: %s",
                                 faulty_deps, dep_mod_names)

    def parse_version_range(self, version_spec):
        """Parse provided version specification as a version range."""
        res = {}
        range_sep = ':'  # version range separator (e.g. ]1.0:2.0])

        if range_sep in version_spec:
            # remove range characters to obtain lower/upper version limits
            version_limits = version_spec.translate(None, '][').split(range_sep)
            if len(version_limits) == 2:
                res['lower'], res['upper'] = version_limits
                if res['lower'] and res['upper'] and LooseVersion(res['lower']) > LooseVersion('upper'):
                    raise EasyBuildError("Incorrect version range, found lower limit > higher limit: %s", version_spec)
            else:
                raise EasyBuildError("Incorrect version range, expected lower/upper limit: %s", version_spec)

            res['excl_lower'] = version_spec[0] == ']'
            res['excl_upper'] = version_spec[-1] == '['

        else:  # strict version spec (not a range)
            res['lower'] = res['upper'] = version_spec
            res['excl_lower'] = res['excl_upper'] = False

        return res

    def parse_filter_deps(self):
        """Parse specifications for which dependencies should be filtered."""
        res = {}

        separator = '='
        for filter_dep_spec in build_option('filter_deps') or []:
            if separator in filter_dep_spec:
                dep_specs = filter_dep_spec.split(separator)
                if len(dep_specs) == 2:
                    dep_name, dep_version_spec = dep_specs
                else:
                    raise EasyBuildError("Incorrect specification for dependency to filter: %s", filter_dep_spec)

                res[dep_name] = self.parse_version_range(dep_version_spec)
            else:
                res[filter_dep_spec] = {'always_filter': True}

        return res

    def filter_deps(self, deps):
        """Filter dependencies according to 'filter-deps' configuration setting."""

        retained_deps = []
        filter_deps_specs = self.parse_filter_deps()

        for dep in deps:
            filter_dep = False

            # figure out whether this dependency should be filtered
            if dep['name'] in filter_deps_specs:

                filter_spec = filter_deps_specs[dep['name']]

                if filter_spec.get('always_filter', False):
                    filter_dep = True
                else:
                    version = LooseVersion(dep['version'])
                    lower = LooseVersion(filter_spec['lower']) if filter_spec['lower'] else None
                    upper = LooseVersion(filter_spec['upper']) if filter_spec['upper'] else None

                    # assume dep is filtered before checking version range

                    filter_dep = True

                    # if version is lower than lower limit: no filtering
                    if lower:
                        if version < lower or (filter_spec['excl_lower'] and version == lower):
                            filter_dep = False

                    # if version is higher than upper limit: no filtering
                    if upper:
                        if version > upper or (filter_spec['excl_upper'] and version == upper):
                            filter_dep = False

            if filter_dep:
                self.log.info("filtered out dependency %s", dep)
            else:
                retained_deps.append(dep)

        return retained_deps

    def dependencies(self, build_only=False):
        """
        Returns an array of parsed dependencies (after filtering, if requested)
        dependency = {'name': '', 'version': '', 'dummy': (False|True), 'versionsuffix': '', 'toolchain': ''}

        :param build_only: only return build dependencies, discard others
        """
        if build_only:
            deps = self['builddependencies']
        else:
            deps = self['dependencies'] + self['builddependencies'] + self['hiddendependencies']

        # if filter-deps option is provided we "clean" the list of dependencies for
        # each processed easyconfig to remove the unwanted dependencies
        self.log.debug("Dependencies BEFORE filtering: %s", deps)

        retained_deps = self.filter_deps(deps)
        self.log.debug("Dependencies AFTER filtering: %s", retained_deps)

        return retained_deps

    def builddependencies(self):
        """
        return the parsed build dependencies
        """
        return self['builddependencies']

    @property
    def name(self):
        """
        returns name
        """
        return self['name']

    @property
    def version(self):
        """
        returns version
        """
        return self['version']

    @property
    def toolchain(self):
        """
        returns the Toolchain used
        """
        if self._toolchain is None:
            # provide list of (direct) toolchain dependencies (name & version), if easyconfig can be found for toolchain
            tcdeps = None
            tcname, tcversion = self['toolchain']['name'], self['toolchain']['version']

            if tcname != DUMMY_TOOLCHAIN_NAME:
                tc_ecfile = robot_find_easyconfig(tcname, tcversion)
                if tc_ecfile is None:
                    self.log.debug("No easyconfig found for toolchain %s version %s, can't determine dependencies",
                                   tcname, tcversion)
                else:
                    self.log.debug("Found easyconfig for toolchain %s version %s: %s", tcname, tcversion, tc_ecfile)
                    tc_ec = process_easyconfig(tc_ecfile)[0]
                    tcdeps = tc_ec['ec'].dependencies()
                    self.log.debug("Toolchain dependencies based on easyconfig: %s", tcdeps)

            self._toolchain = get_toolchain(self['toolchain'], self['toolchainopts'],
                                            mns=ActiveMNS(), tcdeps=tcdeps, modtool=self.modules_tool)
            tc_dict = self._toolchain.as_dict()
            self.log.debug("Initialized toolchain: %s (opts: %s)" % (tc_dict, self['toolchainopts']))
        return self._toolchain

    @property
    def all_dependencies(self):
        """Return list of all dependencies, incl. hidden/build deps & toolchain, but excluding filtered deps."""
        if self._all_dependencies is None:
            self.log.debug("Composing list of all dependencies (incl. toolchain)")
            self._all_dependencies = copy.deepcopy(self.dependencies())
            if self['toolchain']['name'] != DUMMY_TOOLCHAIN_NAME:
                self._all_dependencies.append(self.toolchain.as_dict())

        return self._all_dependencies

    def dump(self, fp, always_overwrite=True, backup=False):
        """
        Dump this easyconfig to file, with the given filename.

        :param always_overwrite: overwrite existing file at specified location without use of --force
        :param backup: create backup of existing file before overwriting it
        """
        orig_enable_templating = self.enable_templating

        # templated values should be dumped unresolved
        self.enable_templating = False

        # build dict of default values
        default_values = dict([(key, DEFAULT_CONFIG[key][0]) for key in DEFAULT_CONFIG])
        default_values.update(dict([(key, self.extra_options[key][0]) for key in self.extra_options]))

        self.generate_template_values()
        templ_const = dict([(quote_py_str(const[1]), const[0]) for const in TEMPLATE_CONSTANTS])

        # create reverse map of templates, to inject template values where possible
        # longer template values are considered first, shorter template keys get preference over longer ones
        sorted_keys = sorted(self.template_values, key=lambda k: (len(self.template_values[k]), -len(k)), reverse=True)
        templ_val = OrderedDict([])
        for key in sorted_keys:
            # shortest template 'key' is retained in case of duplicates ('namelower' is preferred over 'github_account')
            # only template values longer than 2 characters are retained
            if self.template_values[key] not in templ_val and len(self.template_values[key]) > 2:
                templ_val[self.template_values[key]] = key

        try:
            ectxt = self.parser.dump(self, default_values, templ_const, templ_val)
        except NotImplementedError as err:
            # need to restore enable_templating value in case this method is caught in a try/except block and ignored
            # (the ability to dump is not a hard requirement for build success)
            self.enable_templating = orig_enable_templating
            raise NotImplementedError(err)

        self.log.debug("Dumped easyconfig: %s", ectxt)

        if build_option('dump_autopep8'):
            autopep8_opts = {
                'aggressive': 1,  # enable non-whitespace changes, but don't be too aggressive
                'max_line_length': 120,
            }
            self.log.info("Reformatting dumped easyconfig using autopep8 (options: %s)", autopep8_opts)
            ectxt = autopep8.fix_code(ectxt, options=autopep8_opts)
            self.log.debug("Dumped easyconfig after autopep8 reformatting: %s", ectxt)

        write_file(fp, ectxt, always_overwrite=always_overwrite, backup=backup, verbose=backup)

        self.enable_templating = orig_enable_templating

    def _validate(self, attr, values):  # private method
        """
        validation helper method. attr is the attribute it will check, values are the possible values.
        if the value of the attribute is not in the is array, it will report an error
        """
        if values is None:
            values = []
        if self[attr] and self[attr] not in values:
            raise EasyBuildError("%s provided '%s' is not valid: %s", attr, self[attr], values)

    def handle_external_module_metadata(self, dep_name):
        """
        helper function for _parse_dependency
        handles metadata for external module dependencies
        """
        dependency = {}
        if dep_name in self.external_modules_metadata:
            dependency['external_module_metadata'] = self.external_modules_metadata[dep_name]
            self.log.info("Updated dependency info with available metadata for external module %s: %s",
                          dep_name, dependency['external_module_metadata'])
        else:
            self.log.info("No metadata available for external module %s", dep_name)

        return dependency

    # private method
    def _parse_dependency(self, dep, hidden=False, build_only=False):
        """
        parses the dependency into a usable dict with a common format
        dep can be a dict, a tuple or a list.
        if it is a tuple or a list the attributes are expected to be in the following order:
        ('name', 'version', 'versionsuffix', 'toolchain')
        of these attributes, 'name' and 'version' are mandatory

        output dict contains these attributes:
        ['name', 'version', 'versionsuffix', 'dummy', 'toolchain', 'short_mod_name', 'full_mod_name', 'hidden',
         'external_module']

        :param hidden: indicate whether corresponding module file should be installed hidden ('.'-prefixed)
        :param build_only: indicate whether this is a build-only dependency
        """
        # convert tuple to string otherwise python might complain about the formatting
        self.log.debug("Parsing %s as a dependency" % str(dep))

        attr = ['name', 'version', 'versionsuffix', 'toolchain']
        dependency = {
            # full/short module names
            'full_mod_name': None,
            'short_mod_name': None,
            # software name, version, versionsuffix
            'name': None,
            'version': None,
            'versionsuffix': '',
            # toolchain with which this dependency is installed
            'toolchain': None,
            'toolchain_inherited': False,
            # boolean indicating whether we're dealing with a dummy toolchain for this dependency
            'dummy': False,
            # boolean indicating whether the module for this dependency is (to be) installed hidden
            'hidden': hidden,
            # boolean indicating whether this this a build-only dependency
            'build_only': build_only,
            # boolean indicating whether this dependency should be resolved via an external module
            'external_module': False,
            # metadata in case this is an external module;
            # provides information on what this module represents (software name/version, install prefix, ...)
            'external_module_metadata': {},
        }
        if isinstance(dep, dict):
            dependency.update(dep)

            # make sure 'dummy' key is handled appropriately
            if 'dummy' in dep and 'toolchain' not in dep:
                dependency['toolchain'] = dep['dummy']

            if dep.get('external_module', False):
                dependency.update(self.handle_external_module_metadata(dep['full_mod_name']))

        elif isinstance(dep, Dependency):
            dependency['name'] = dep.name()
            dependency['version'] = dep.version()
            versionsuffix = dep.versionsuffix()
            if versionsuffix is not None:
                dependency['versionsuffix'] = versionsuffix
            toolchain = dep.toolchain()
            if toolchain is not None:
                dependency['toolchain'] = toolchain

        elif isinstance(dep, (list, tuple)):
            if dep and dep[-1] == EXTERNAL_MODULE_MARKER:
                if len(dep) == 2:
                    dependency['external_module'] = True
                    dependency['short_mod_name'] = dep[0]
                    dependency['full_mod_name'] = dep[0]
                    dependency.update(self.handle_external_module_metadata(dep[0]))
                else:
                    raise EasyBuildError("Incorrect external dependency specification: %s", dep)
            else:
                # non-external dependency: tuple (or list) that specifies name/version(/versionsuffix(/toolchain))
                dependency.update(dict(zip(attr, dep)))

        else:
            raise EasyBuildError("Dependency %s of unsupported type: %s", dep, type(dep))

        if dependency['external_module']:
            # check whether the external module is hidden
            if dependency['full_mod_name'].split('/')[-1].startswith('.'):
                dependency['hidden'] = True

            self.log.debug("Returning parsed external dependency: %s", dependency)
            return dependency

        # check whether this dependency should be hidden according to --hide-deps
        if build_option('hide_deps'):
            dependency['hidden'] |= dependency['name'] in build_option('hide_deps')

        # dependency inherits toolchain, unless it's specified to have a custom toolchain
        tc = copy.deepcopy(self['toolchain'])
        tc_spec = dependency['toolchain']
        if tc_spec is None:
            self.log.debug("Inheriting parent toolchain %s for dep %s (until deps are finalised)", tc, dependency)
            dependency['toolchain_inherited'] = True

        # (true) boolean value simply indicates that a dummy toolchain is used
        elif isinstance(tc_spec, bool) and tc_spec:
                tc = {'name': DUMMY_TOOLCHAIN_NAME, 'version': DUMMY_TOOLCHAIN_VERSION}

        # two-element list/tuple value indicates custom toolchain specification
        elif isinstance(tc_spec, (list, tuple,)):
            if len(tc_spec) == 2:
                tc = {'name': tc_spec[0], 'version': tc_spec[1]}
            else:
                raise EasyBuildError("List/tuple value for toolchain should have two elements (%s)", str(tc_spec))

        elif isinstance(tc_spec, dict):
            if 'name' in tc_spec and 'version' in tc_spec:
                tc = copy.deepcopy(tc_spec)
            else:
                raise EasyBuildError("Found toolchain spec as dict with wrong keys (no name/version): %s", tc_spec)

        else:
            raise EasyBuildError("Unsupported type for toolchain spec encountered: %s (%s)", tc_spec, type(tc_spec))

        self.log.debug("Derived toolchain to use for dependency %s, based on toolchain spec %s: %s", dep, tc_spec, tc)
        dependency['toolchain'] = tc

        # validations
        if dependency['name'] is None:
            raise EasyBuildError("Dependency specified without name: %s", dependency)

        if dependency['version'] is None:
            raise EasyBuildError("Dependency specified without version: %s", dependency)

        return dependency

    def _finalize_dependencies(self):
        """Finalize dependency parameters, after initial parsing."""

        filter_deps = build_option('filter_deps')

        for key in DEPENDENCY_PARAMETERS:
            # loop over a *copy* of dependency dicts (with resolved templates);
            # to update the original dep dict, we need to index with idx into self._config[key][0]...
            for idx, dep in enumerate(self[key]):

                # reference to original dep dict, this is the one we should be updating
                orig_dep = self._config[key][0][idx]

                if filter_deps and orig_dep['name'] in filter_deps:
                    self.log.debug("Skipping filtered dependency %s when finalising dependencies", orig_dep['name'])
                    continue

                # handle dependencies with inherited (non-dummy) toolchain
                # this *must* be done after parsing all dependencies, to avoid problems with templates like %(pyver)s
                if dep['toolchain_inherited'] and dep['toolchain']['name'] != DUMMY_TOOLCHAIN_NAME:
                    tc = None
                    dep_str = '%s %s%s' % (dep['name'], dep['version'], dep['versionsuffix'])
                    self.log.debug("Figuring out toolchain to use for dep %s...", dep)
                    if build_option('minimal_toolchains'):
                        # determine 'smallest' subtoolchain for which a matching easyconfig file is available
                        self.log.debug("Looking for minimal toolchain for dependency %s (parent toolchain: %s)...",
                                       dep_str, dep['toolchain'])
                        tc = robot_find_subtoolchain_for_dep(dep, self.modules_tool)
                        if tc is None:
                            raise EasyBuildError("Failed to determine minimal toolchain for dep %s", dep_str)
                    else:
                        # try to determine subtoolchain for dep;
                        # this is done considering both available modules and easyconfigs (in that order)
                        tc = robot_find_subtoolchain_for_dep(dep, self.modules_tool, parent_first=True)
                        self.log.debug("Using subtoolchain %s for dep %s", tc, dep_str)

                    if tc is None:
                        self.log.debug("Inheriting toolchain %s from parent for dep %s", dep['toolchain'], dep_str)
                    else:
                        # put derived toolchain in place
                        self.log.debug("Figured out toolchain to use for dep %s: %s", dep_str, tc)
                        dep['toolchain'] = orig_dep['toolchain'] = tc
                        dep['toolchain_inherited'] = orig_dep['toolchain_inherited'] = False

                if not dep['external_module']:
                    # make sure 'dummy' is set correctly
                    orig_dep['dummy'] = dep['toolchain']['name'] == DUMMY_TOOLCHAIN_NAME

                    # set module names
                    orig_dep['short_mod_name'] = ActiveMNS().det_short_module_name(dep)
                    orig_dep['full_mod_name'] = ActiveMNS().det_full_module_name(dep)

    def generate_template_values(self):
        """Try to generate all template values."""

        self._generate_template_values(skip_lower=True)
        self._generate_template_values(skip_lower=False)

        # recursive call, until there are no more changes to template values;
        # important since template values may include other templates
        cont = True
        while cont:
            cont = False
            for key in self.template_values:
                try:
                    curr_val = self.template_values[key]
                    new_val = curr_val % self.template_values
                    if new_val != curr_val:
                        cont = True
                    self.template_values[key] = new_val
                except KeyError:
                    # KeyError's may occur when not all templates are defined yet, but these are safe to ignore
                    pass

    def _generate_template_values(self, ignore=None, skip_lower=True):
        """Actual code to generate the template values"""
        if self.template_values is None:
            self.template_values = {}

        # step 0. self.template_values can/should be updated from outside easyconfig
        # (eg the run_setp code in EasyBlock)

        # step 1-3 work with easyconfig.templates constants
        # disable templating with creating dict with template values to avoid looping back to here via __getitem__
        prev_enable_templating = self.enable_templating
        self.enable_templating = False
        template_values = template_constant_dict(self, ignore=ignore, skip_lower=skip_lower)
        self.enable_templating = prev_enable_templating

        # update the template_values dict
        self.template_values.update(template_values)

        # cleanup None values
        for k, v in self.template_values.items():
            if v is None:
                del self.template_values[k]

    @handle_deprecated_or_replaced_easyconfig_parameters
    def __contains__(self, key):
        """Check whether easyconfig parameter is defined"""
        return key in self._config

    @handle_deprecated_or_replaced_easyconfig_parameters
    def __getitem__(self, key):
        """Return value of specified easyconfig parameter (without help text, etc.)"""
        value = None
        if key in self._config:
            value = self._config[key][0]
        else:
            raise EasyBuildError("Use of unknown easyconfig parameter '%s' when getting parameter value", key)

        if self.enable_templating:
            if self.template_values is None or len(self.template_values) == 0:
                self.generate_template_values()
            value = resolve_template(value, self.template_values)

        return value

    @handle_deprecated_or_replaced_easyconfig_parameters
    def __setitem__(self, key, value):
        """Set value of specified easyconfig parameter (help text & co is left untouched)"""
        if key in self._config:
            self._config[key][0] = value
        else:
            raise EasyBuildError("Use of unknown easyconfig parameter '%s' when setting parameter value to '%s'",
                                 key, value)

    @handle_deprecated_or_replaced_easyconfig_parameters
    def get(self, key, default=None):
        """
        Gets the value of a key in the config, with 'default' as fallback.
        """
        if key in self:
            return self[key]
        else:
            return default

    # *both* __eq__ and __ne__ must be implemented for == and != comparisons to work correctly
    # see also https://docs.python.org/2/reference/datamodel.html#object.__eq__
    def __eq__(self, ec):
        """Is this EasyConfig instance equivalent to the provided one?"""
        return self.asdict() == ec.asdict()

    def __ne__(self, ec):
        """Is this EasyConfig instance equivalent to the provided one?"""
        return self.asdict() != ec.asdict()

    def __hash__(self):
        """Return hash value for a hashable representation of this EasyConfig instance."""
        def make_hashable(val):
            """Make a hashable value of the given value."""
            if isinstance(val, list):
                val = tuple([make_hashable(x) for x in val])
            elif isinstance(val, dict):
                val = tuple([(key, make_hashable(val)) for (key, val) in sorted(val.items())])
            return val

        lst = []
        for (key, val) in sorted(self.asdict().items()):
            lst.append((key, make_hashable(val)))

        # a list is not hashable, but a tuple is
        return hash(tuple(lst))

    def asdict(self):
        """
        Return dict representation of this EasyConfig instance.
        """
        res = {}
        for key, tup in self._config.items():
            value = tup[0]
            if self.enable_templating:
                if not self.template_values:
                    self.generate_template_values()
                value = resolve_template(value, self.template_values)
            res[key] = value
        return res


def det_installversion(version, toolchain_name, toolchain_version, prefix, suffix):
    """Deprecated 'det_installversion' function, to determine exact install version, based on supplied parameters."""
    old_fn = 'framework.easyconfig.easyconfig.det_installversion'
    _log.nosupport('Use det_full_ec_version from easybuild.tools.module_generator instead of %s' % old_fn, '2.0')


def get_easyblock_class(easyblock, name=None, error_on_failed_import=True, error_on_missing_easyblock=None, **kwargs):
    """
    Get class for a particular easyblock (or use default)
    """
    if 'default_fallback' in kwargs:
        msg = "Named argument 'default_fallback' for get_easyblock_class is deprecated, "
        msg += "use 'error_on_missing_easyblock' instead"
        _log.deprecated(msg, '4.0')
        if error_on_missing_easyblock is None:
            error_on_missing_easyblock = kwargs['default_fallback']
    elif error_on_missing_easyblock is None:
        error_on_missing_easyblock = True

    cls = None
    try:
        if easyblock:
            # something was specified, lets parse it
            es = easyblock.split('.')
            class_name = es.pop(-1)
            # figure out if full path was specified or not
            if es:
                modulepath = '.'.join(es)
                _log.info("Assuming that full easyblock module path was specified (class: %s, modulepath: %s)",
                          class_name, modulepath)
                cls = get_class_for(modulepath, class_name)
            else:
                modulepath = get_module_path(easyblock)
                cls = get_class_for(modulepath, class_name)
                _log.info("Derived full easyblock module path for %s: %s" % (class_name, modulepath))
        else:
            # if no easyblock specified, try to find if one exists
            if name is None:
                name = "UNKNOWN"
            # The following is a generic way to calculate unique class names for any funny software title
            class_name = encode_class_name(name)
            # modulepath will be the namespace + encoded modulename (from the classname)
            modulepath = get_module_path(class_name, generic=False)
            modulepath_imported = False
            try:
                __import__(modulepath, globals(), locals(), [''])
                modulepath_imported = True
            except ImportError, err:
                _log.debug("Failed to import module '%s': %s" % (modulepath, err))

            # check if determining module path based on software name would have resulted in a different module path
            if modulepath_imported:
                _log.debug("Module path '%s' found" % modulepath)
            else:
                _log.debug("No module path '%s' found" % modulepath)
                modulepath_bis = get_module_path(name, generic=False, decode=False)
                _log.debug("Module path determined based on software name: %s" % modulepath_bis)
                if modulepath_bis != modulepath:
                    _log.nosupport("Determining module path based on software name", '2.0')

            # try and find easyblock
            try:
                _log.debug("getting class for %s.%s" % (modulepath, class_name))
                cls = get_class_for(modulepath, class_name)
                _log.info("Successfully obtained %s class instance from %s" % (class_name, modulepath))
            except ImportError, err:
                # when an ImportError occurs, make sure that it's caused by not finding the easyblock module,
                # and not because of a broken import statement in the easyblock module
                error_re = re.compile(r"No module named %s" % modulepath.replace("easybuild.easyblocks.", ''))
                _log.debug("error regexp: %s" % error_re.pattern)
                if error_re.match(str(err)):
                    if error_on_missing_easyblock:
                        raise EasyBuildError("No software-specific easyblock '%s' found for %s", class_name, name)
                elif error_on_failed_import:
                    raise EasyBuildError("Failed to import %s easyblock: %s", class_name, err)
                else:
                    _log.debug("Failed to import easyblock for %s, but ignoring it: %s" % (class_name, err))

        if cls is not None:
            _log.info("Successfully obtained class '%s' for easyblock '%s' (software name '%s')",
                      cls.__name__, easyblock, name)
        else:
            _log.debug("No class found for easyblock '%s' (software name '%s')", easyblock, name)

        return cls

    except EasyBuildError, err:
        # simply reraise rather than wrapping it into another error
        raise err
    except Exception, err:
        raise EasyBuildError("Failed to obtain class for %s easyblock (not available?): %s", easyblock, err)


def get_module_path(name, generic=None, decode=True):
    """
    Determine the module path for a given easyblock or software name,
    based on the encoded class name.

    :param generic: whether or not the easyblock is generic (if None: auto-derive from specified class name)
    :param decode: whether or not to decode the provided class name
    """
    if name is None:
        return None

    if generic is None:
        generic = not name.startswith(EASYBLOCK_CLASS_PREFIX)

    # example: 'EB_VSC_minus_tools' should result in 'vsc_tools'
    if decode:
        name = decode_class_name(name)
    module_name = remove_unwanted_chars(name.replace('-', '_')).lower()

    modpath = ['easybuild', 'easyblocks']
    if generic:
        modpath.append('generic')

    return '.'.join(modpath + [module_name])


def resolve_template(value, tmpl_dict):
    """Given a value, try to susbstitute the templated strings with actual values.
        - value: some python object (supported are string, tuple/list, dict or some mix thereof)
        - tmpl_dict: template dictionary
    """
    if isinstance(value, basestring):
        # simple escaping, making all '%foo', '%%foo', '%%%foo' post-templates values available,
        #         but ignore a string like '%(name)s'
        # behaviour of strings like '%(name)s',
        #   make sure that constructs like %%(name)s are preserved
        #   higher order escaping in the original text is considered advanced users only,
        #   and a big no-no otherwise. It indicates that want some new functionality
        #   in easyconfigs, so just open an issue for it.
        #   detailed behaviour:
        #     if a an odd number of % prefixes the (name)s,
        #     we assume that templating is assumed and the behaviour is as follows
        #     '%(name)s' -> '%(name)s', and after templating with {'name':'x'} -> 'x'
        #     '%%%(name)s' -> '%%%(name)s', and after templating with {'name':'x'} -> '%x'
        #     if a an even number of % prefixes the (name)s,
        #     we assume that no templating is desired and the behaviour is as follows
        #     '%%(name)s' -> '%%(name)s', and after templating with {'name':'x'} -> '%(name)s'
        #     '%%%%(name)s' -> '%%%%(name)s', and after templating with {'name':'x'} -> '%%(name)s'
        # examples:
        # '10%' -> '10%%'
        # '%s' -> '%%s'
        # '%%' -> '%%%%'
        # '%(name)s' -> '%(name)s'
        # '%%(name)s' -> '%%(name)s'
        if '%' in value:
            value = re.sub(re.compile(r'(%)(?!%*\(\w+\)s)'), r'\1\1', value)

            try:
                value = value % tmpl_dict
            except KeyError:
                _log.warning("Unable to resolve template value %s with dict %s", value, tmpl_dict)
    else:
        # this block deals with references to objects and returns other references
        # for reading this is ok, but for self['x'] = {}
        # self['x']['y'] = z does not work
        # self['x'] is a get, will return a reference to a templated version of self._config['x']
        # and the ['y] = z part will be against this new reference
        # you will need to do
        # self.enable_templating = False
        # self['x']['y'] = z
        # self.enable_templating = True
        # or (direct but evil)
        # self._config['x']['y'] = z
        # it can not be intercepted with __setitem__ because the set is done at a deeper level
        if isinstance(value, list):
            value = [resolve_template(val, tmpl_dict) for val in value]
        elif isinstance(value, tuple):
            value = tuple(resolve_template(list(value), tmpl_dict))
        elif isinstance(value, dict):
            value = dict((resolve_template(k, tmpl_dict), resolve_template(v, tmpl_dict)) for k, v in value.items())

    return value


def process_easyconfig(path, build_specs=None, validate=True, parse_only=False, hidden=None):
    """
    Process easyconfig, returning some information for each block
    :param path: path to easyconfig file
    :param build_specs: dictionary specifying build specifications (e.g. version, toolchain, ...)
    :param validate: whether or not to perform validation
    :param parse_only: only parse easyconfig superficially (faster, but results in partial info)
    :param hidden: indicate whether corresponding module file should be installed hidden ('.'-prefixed)
    """
    blocks = retrieve_blocks_in_spec(path, build_option('only_blocks'))

    if hidden is None:
        hidden = build_option('hidden')

    # only cache when no build specifications are involved (since those can't be part of a dict key)
    cache_key = None
    if build_specs is None:
        cache_key = (path, validate, hidden, parse_only)
        if cache_key in _easyconfigs_cache:
            return [e.copy() for e in _easyconfigs_cache[cache_key]]

    easyconfigs = []
    for spec in blocks:
        # process for dependencies and real installversionname
        _log.debug("Processing easyconfig %s" % spec)

        # create easyconfig
        try:
            ec = EasyConfig(spec, build_specs=build_specs, validate=validate, hidden=hidden)
        except EasyBuildError, err:
            raise EasyBuildError("Failed to process easyconfig %s: %s", spec, err.msg)

        name = ec['name']

        easyconfig = {
            'ec': ec,
        }
        easyconfigs.append(easyconfig)

        if not parse_only:
            # also determine list of dependencies, module name (unless only parsed easyconfigs are requested)
            easyconfig.update({
                'spec': ec.path,
                'short_mod_name': ec.short_mod_name,
                'full_mod_name': ec.full_mod_name,
                'dependencies': [],
                'builddependencies': [],
                'hiddendependencies': [],
                'hidden': ec.hidden,
            })
            if len(blocks) > 1:
                easyconfig['original_spec'] = path

            # add build dependencies
            for dep in ec['builddependencies']:
                _log.debug("Adding build dependency %s for app %s." % (dep, name))
                easyconfig['builddependencies'].append(dep)

            # add hidden dependencies
            for dep in ec['hiddendependencies']:
                _log.debug("Adding hidden dependency %s for app %s." % (dep, name))
                easyconfig['hiddendependencies'].append(dep)

            # add dependencies (including build & hidden dependencies)
            for dep in ec.dependencies():
                _log.debug("Adding dependency %s for app %s." % (dep, name))
                easyconfig['dependencies'].append(dep)

            # add toolchain as dependency too
            if ec['toolchain']['name'] != DUMMY_TOOLCHAIN_NAME:
                tc = ec.toolchain.as_dict()
                _log.debug("Adding toolchain %s as dependency for app %s." % (tc, name))
                easyconfig['dependencies'].append(tc)

    if cache_key is not None:
        _easyconfigs_cache[cache_key] = [e.copy() for e in easyconfigs]

    return easyconfigs


def letter_dir_for(name):
    """
    Determine 'letter' directory for specified software name.
    This usually just the 1st letter of the software name (in lowercase),
    except for funky software names, e.g. ones starting with a digit.
    """
    # wildcard name should result in wildcard letter
    if name == '*':
        letter = '*'
    else:
        letter = name.lower()[0]
        # outside of a-z range, use '0'
        if letter < 'a' or letter > 'z':
            letter = '0'

    return letter


def create_paths(path, name, version):
    """
    Returns all the paths where easyconfig could be located
    <path> is the basepath
    <name> should be a string
    <version> can be a '*' if you use glob patterns, or an install version otherwise
    """
    cand_paths = [
        (name, version),  # e.g. <path>/GCC/4.8.2.eb
        (name, '%s-%s' % (name, version)),  # e.g. <path>/GCC/GCC-4.8.2.eb
        (letter_dir_for(name), name, '%s-%s' % (name, version)),  # e.g. <path>/g/GCC/GCC-4.8.2.eb
        ('%s-%s' % (name, version),),  # e.g. <path>/GCC-4.8.2.eb
    ]
    return ['%s.eb' % os.path.join(path, *cand_path) for cand_path in cand_paths]


def robot_find_easyconfig(name, version):
    """
    Find an easyconfig for module in path, returns (absolute) path to easyconfig file (or None, if none is found).
    """
    key = (name, version)
    if key in _easyconfig_files_cache:
        _log.debug("Obtained easyconfig path from cache for %s: %s" % (key, _easyconfig_files_cache[key]))
        return _easyconfig_files_cache[key]

    paths = build_option('robot_path')
    if paths is None:
        paths = []
    elif not isinstance(paths, (list, tuple)):
        paths = [paths]

    # if we should also consider archived easyconfigs, duplicate paths list with archived equivalents
    if build_option('consider_archived_easyconfigs'):
        paths = paths + [os.path.join(p, EASYCONFIGS_ARCHIVE_DIR) for p in paths]

    res = None
    for path in paths:
        easyconfigs_paths = create_paths(path, name, version)
        for easyconfig_path in easyconfigs_paths:
            _log.debug("Checking easyconfig path %s" % easyconfig_path)
            if os.path.isfile(easyconfig_path):
                _log.debug("Found easyconfig file for name %s, version %s at %s" % (name, version, easyconfig_path))
                _easyconfig_files_cache[key] = os.path.abspath(easyconfig_path)
                res = _easyconfig_files_cache[key]
                break
        if res:
            break

    return res


def verify_easyconfig_filename(path, specs, parsed_ec=None):
    """
    Check whether parsed easyconfig at specified path matches expected specs;
    this basically verifies whether the easyconfig filename corresponds to its contents

    :param path: path to easyconfig file
    :param specs: expected specs (dict with easyconfig parameter values)
    :param parsed_ec: (list of) EasyConfig instance(s) corresponding to easyconfig file
    """
    if isinstance(parsed_ec, EasyConfig):
        ecs = [{'ec': parsed_ec}]
    elif isinstance(parsed_ec, (list, tuple)):
        ecs = parsed_ec
    elif parsed_ec is None:
        ecs = process_easyconfig(path)
    else:
        raise EasyBuildError("Unexpected value type for parsed_ec: %s (%s)", type(parsed_ec), parsed_ec)

    fullver = det_full_ec_version(specs)

    expected_filename = '%s-%s.eb' % (specs['name'], fullver)
    if os.path.basename(path) != expected_filename:
        # only retain relevant specs to produce a more useful error message
        specstr = ''
        for key in ['name', 'version', 'versionsuffix']:
            specstr += "%s: %s; " % (key, quote_py_str(specs.get(key)))
        toolchain = specs.get('toolchain')
        if toolchain:
            tcname, tcver = quote_py_str(toolchain.get('name')), quote_py_str(toolchain.get('version'))
            specstr += "toolchain name, version: %s, %s" % (tcname, tcver)
        else:
            specstr += "toolchain: None"

        raise EasyBuildError("Easyconfig filename '%s' does not match with expected filename '%s' (specs: %s)",
                             os.path.basename(path), expected_filename, specstr)

    for ec in ecs:
        found_fullver = det_full_ec_version(ec['ec'])
        if ec['ec']['name'] != specs['name'] or found_fullver != fullver:
            subspec = dict((key, specs[key]) for key in ['name', 'toolchain', 'version', 'versionsuffix'])
            error_msg = "Contents of %s does not match with filename" % path
            error_msg += "; expected filename based on contents: %s-%s.eb" % (ec['ec']['name'], found_fullver)
            error_msg += "; expected (relevant) parameters based on filename %s: %s" % (os.path.basename(path), subspec)
            raise EasyBuildError(error_msg)

    _log.info("Contents of %s verified against easyconfig filename, matches %s", path, specs)


def robot_find_subtoolchain_for_dep(dep, modtool, parent_tc=None, parent_first=False):
    """
    Find the subtoolchain to use for a dependency

    :param dep: dependency target dict (long and short module names may not exist yet)
    :param parent_tc: toolchain from which to derive the toolchain hierarchy to search (default: use dep's toolchain)
    :param parent_first: reverse order in which subtoolchains are considered: parent toolchain, then subtoolchains
    :return: minimal toolchain for which an easyconfig exists for this dependency (and matches build_options)
    """
    if parent_tc is None:
        parent_tc = dep['toolchain']

    retain_all_deps = build_option('retain_all_deps')
    use_existing_modules = build_option('use_existing_modules') and not retain_all_deps

    if parent_first or use_existing_modules:
        avail_modules = modtool.available()
    else:
        avail_modules = []

    newdep = copy.deepcopy(dep)

    # try to determine toolchain hierarchy
    # this may fail if not all easyconfig files that define this toolchain are available,
    # but that's not always fatal: it's mostly irrelevant under --review-pr for example
    try:
        toolchain_hierarchy = get_toolchain_hierarchy(parent_tc)
    except EasyBuildError as err:
        warning_msg = "Failed to determine toolchain hierarchy for %(name)s/%(version)s when determining " % parent_tc
        warning_msg += "subtoolchain for dependency '%s': %s" % (dep['name'], err)
        _log.warning(warning_msg)
        print_warning(warning_msg)
        toolchain_hierarchy = []

    # start with subtoolchains first, i.e. first (dummy or) compiler-only toolchain, etc.,
    # unless parent toolchain should be considered first
    if parent_first:
        toolchain_hierarchy = toolchain_hierarchy[::-1]

    cand_subtcs = []

    for tc in toolchain_hierarchy:
        # try to determine module name using this particular subtoolchain;
        # this may fail if no easyconfig is available in robot search path
        # and the module naming scheme requires an easyconfig file
        newdep['toolchain'] = tc
        mod_name = ActiveMNS().det_full_module_name(newdep, require_result=False)

        # if the module name can be determined, subtoolchain is an actual candidate
        if mod_name:
            # check whether module already exists or not (but only if that info will actually be used)
            mod_exists = None
            if parent_first or use_existing_modules:
                mod_exists = mod_name in avail_modules
                # fallback to checking with modtool.exist is required,
                # for hidden modules and external modules where module name may be partial
                if not mod_exists:
                    maybe_partial = dep.get('external_module', True)
                    mod_exists = modtool.exist([mod_name], skip_avail=True, maybe_partial=maybe_partial)[0]

            # add the subtoolchain to list of candidates
            cand_subtcs.append({'toolchain': tc, 'mod_exists': mod_exists})

    _log.debug("List of possible subtoolchains for %s: %s", dep, cand_subtcs)

    cand_subtcs_with_mod = [tc for tc in cand_subtcs if tc.get('mod_exists', False)]

    # scenario I:
    # - regardless of whether minimal toolchains mode is enabled or not
    # - try to pick subtoolchain based on available easyconfigs (first hit wins)
    minimal_toolchain = None
    for cand_subtc in cand_subtcs:
        newdep['toolchain'] = cand_subtc['toolchain']
        ec_file = robot_find_easyconfig(newdep['name'], det_full_ec_version(newdep))
        if ec_file:
            minimal_toolchain = cand_subtc['toolchain']
            break

    if cand_subtcs_with_mod:
        if parent_first:
            # scenario II:
            # - parent toolchain first (minimal toolchains mode *not* enabled)
            # - module for dependency is already available for one of the subtoolchains
            # - only used as fallback in case subtoolchain could not be determined via easyconfigs (scenario I)
            # If so, we retain the subtoolchain closest to the parent (so top of the list of candidates)
            if minimal_toolchain is None or use_existing_modules:
                minimal_toolchain = cand_subtcs_with_mod[0]['toolchain']

        elif use_existing_modules:
            # scenario III:
            # - minimal toolchains mode + --use-existing-modules
            # - reconsider subtoolchain based on already available modules for dependency
            # - this may overrule subtoolchain picked in scenario II

            # take the last element, i.e. the maximum toolchain where a module exists already
            # (allows for potentially better optimisation)
            minimal_toolchain = cand_subtcs_with_mod[-1]['toolchain']

    if minimal_toolchain is None:
        _log.info("Irresolvable dependency found (even with minimal toolchains): %s", dep)

    _log.info("Minimally resolving dependency %s using toolchain %s", dep, minimal_toolchain)
    return minimal_toolchain


def det_location_for(path, target_dir, soft_name, target_file):
    """
    Determine path to easyconfigs directory for specified software name, using specified target file name.

    :param path: path of file to copy
    :param target_dir: (parent) target directory, should contain easybuild/easyconfigs subdirectory
    :param soft_name: software name (to determine location to copy to)
    :param target_file: target file name
    :return: full path to the right location
    """
    subdir = os.path.join('easybuild', 'easyconfigs')

    if os.path.exists(os.path.join(target_dir, subdir)):
        target_path = os.path.join('easybuild', 'easyconfigs', letter_dir_for(soft_name), soft_name, target_file)
        _log.debug("Target path for %s: %s", path, target_path)

        target_path = os.path.join(target_dir, target_path)

    else:
        raise EasyBuildError("Subdirectory %s not found in %s", subdir, target_dir)

    return target_path


def clean_up_easyconfigs(paths):
    """
    Clean up easyconfigs (in place) by filtering out comments/buildstats included by EasyBuild in archived easyconfigs
    (cfr. FileRepository.add_easyconfig in easybuild.tools.repository.filerepo)

    :param paths: list of paths to easyconfigs to clean up
    """
    regexs = [
        re.compile(r"^# Built with EasyBuild.*\n", re.M),
        re.compile(r"^# Build statistics.*\n", re.M),
        # consume buildstats as a whole, i.e. all lines until closing '}]'
        re.compile(r"\n*buildstats\s*=(.|\n)*\n}\]\s*\n?", re.M),
    ]

    for path in paths:
        ectxt = read_file(path)
        for regex in regexs:
            ectxt = regex.sub('', ectxt)
        write_file(path, ectxt, forced=True)


def det_file_info(paths, target_dir):
    """
    Determine useful information on easyconfig files relative to a target directory,
    before any actual operation (e.g. copying) is performed

    :param paths: list of paths to easyconfig files
    :param target_dir: target directory
    :return: dict with useful information on easyconfig files (corresponding EasyConfig instances, paths, status)
             relative to a target directory
    """
    file_info = {
        'ecs': [],
        'paths': [],
        'paths_in_repo': [],
        'new': [],
        'new_folder': [],
        'new_file_in_existing_folder': [],
    }

    for path in paths:
        ecs = process_easyconfig(path, validate=False)
        if len(ecs) == 1:
            file_info['paths'].append(path)
            file_info['ecs'].append(ecs[0]['ec'])

            soft_name = file_info['ecs'][-1].name
            ec_filename = file_info['ecs'][-1].filename()

            target_path = det_location_for(path, target_dir, soft_name, ec_filename)

            new_file = not os.path.exists(target_path)
            new_folder = not os.path.exists(os.path.dirname(target_path))
            file_info['new'].append(new_file)
            file_info['new_folder'].append(new_folder)
            file_info['new_file_in_existing_folder'].append(new_file and not new_folder)
            file_info['paths_in_repo'].append(target_path)

        else:
            raise EasyBuildError("Multiple EasyConfig instances obtained from easyconfig file %s", path)

    return file_info


def copy_easyconfigs(paths, target_dir):
    """
    Copy easyconfig files to specified directory, in the 'right' location and using the filename expected by robot.

    :param paths: list of paths to copy to git working dir
    :param target_dir: target directory
    :return: dict with useful information on copied easyconfig files (corresponding EasyConfig instances, paths, status)
    """
    file_info = det_file_info(paths, target_dir)

    for path, target_path in zip(file_info['paths'], file_info['paths_in_repo']):
        copy_file(path, target_path, force_in_dry_run=True)

    if build_option('cleanup_easyconfigs'):
        clean_up_easyconfigs(file_info['paths_in_repo'])

    return file_info


def copy_patch_files(patch_specs, target_dir):
    """
    Copy patch files to specified directory, in the 'right' location according to the software name they relate to.

    :param patch_specs: list of tuples with patch file location and name of software they are for
    :param target_dir: target directory
    """
    patched_files = {
        'paths_in_repo': [],
    }
    for patch_path, soft_name in patch_specs:
        target_path = det_location_for(patch_path, target_dir, soft_name, os.path.basename(patch_path))
        copy_file(patch_path, target_path, force_in_dry_run=True)
        patched_files['paths_in_repo'].append(target_path)

    return patched_files


class ActiveMNS(object):
    """Wrapper class for active module naming scheme."""

    __metaclass__ = Singleton

    def __init__(self, *args, **kwargs):
        """Initialize logger."""
        self.log = fancylogger.getLogger(self.__class__.__name__, fname=False)

        # determine active module naming scheme
        avail_mnss = avail_module_naming_schemes()
        self.log.debug("List of available module naming schemes: %s" % avail_mnss.keys())
        sel_mns = get_module_naming_scheme()
        if sel_mns in avail_mnss:
            self.mns = avail_mnss[sel_mns]()
        else:
            raise EasyBuildError("Selected module naming scheme %s could not be found in %s",
                                 sel_mns, avail_mnss.keys())

    def requires_full_easyconfig(self, keys):
        """Check whether specified list of easyconfig parameters is sufficient for active module naming scheme."""
        return self.mns.requires_toolchain_details() or not self.mns.is_sufficient(keys)

    def check_ec_type(self, ec, raise_error=True):
        """
        Obtain a full parsed easyconfig file to pass to naming scheme methods if provided keys are insufficient.

        :param ec: available easyconfig parameter specifications (EasyConfig instance or dict value)
        :param raise_error: boolean indicating whether or not an error should be raised
                            if a full easyconfig is required but not found
        """
        if not isinstance(ec, EasyConfig) and self.requires_full_easyconfig(ec.keys()):

            self.log.debug("A parsed easyconfig is required by the module naming scheme, so finding one for %s" % ec)

            # fetch/parse easyconfig file if deemed necessary
            eb_file = robot_find_easyconfig(ec['name'], det_full_ec_version(ec))

            if eb_file is not None:
                parsed_ec = process_easyconfig(eb_file, parse_only=True, hidden=ec['hidden'])
                if len(parsed_ec) > 1:
                    self.log.warning("More than one parsed easyconfig obtained from %s, only retaining first" % eb_file)
                    self.log.debug("Full list of parsed easyconfigs: %s" % parsed_ec)
                ec = parsed_ec[0]['ec']

            elif raise_error:
                raise EasyBuildError("Failed to find easyconfig file '%s-%s.eb' when determining module name for: %s",
                                     ec['name'], det_full_ec_version(ec), ec)
            else:
                self.log.info("No easyconfig found as required by module naming scheme, but not considered fatal")
                ec = None

        return ec

    def _det_module_name_with(self, mns_method, ec, force_visible=False, require_result=True):
        """
        Determine module name using specified module naming scheme method, based on supplied easyconfig.
        Returns a string representing the module name, e.g. 'GCC/4.6.3', 'Python/2.7.5-ictce-4.1.13',
        with the following requirements:
            - module name is specified as a relative path
            - string representing module name has length > 0
            - module name only contains printable characters (string.printable, except carriage-control chars)
        """
        mod_name = None
        ec = self.check_ec_type(ec, raise_error=require_result)

        if ec:
            # replace software name with desired replacement (if specified)
            orig_name = None
            if ec.get('modaltsoftname', None):
                orig_name = ec['name']
                ec['name'] = ec['modaltsoftname']
                self.log.info("Replaced software name '%s' with '%s' when determining module name",
                              orig_name, ec['name'])
            else:
                self.log.debug("No alternative software name specified to determine module name with")

            mod_name = mns_method(ec)

            # restore original software name if it was tampered with
            if orig_name is not None:
                ec['name'] = orig_name

            if not is_valid_module_name(mod_name):
                raise EasyBuildError("%s is not a valid module name", str(mod_name))

            # check whether module name should be hidden or not
            # ec may be either a dict or an EasyConfig instance, 'force_visible' argument overrules
            if (ec.get('hidden', False) or getattr(ec, 'hidden', False)) and not force_visible:
                mod_name = det_hidden_modname(mod_name)

        elif require_result:
            raise EasyBuildError("Failed to determine module name for %s using %s", ec, mns_method)

        return mod_name

    def det_full_module_name(self, ec, force_visible=False, require_result=True):
        """Determine full module name by selected module naming scheme, based on supplied easyconfig."""
        self.log.debug("Determining full module name for %s (force_visible: %s)" % (ec, force_visible))
        if ec.get('external_module', False):
            # external modules have the module name readily available, and may lack the info required by the MNS
            mod_name = ec['full_mod_name']
            self.log.debug("Full module name for external module: %s", mod_name)
        else:
            mod_name = self._det_module_name_with(self.mns.det_full_module_name, ec, force_visible=force_visible,
                                                  require_result=require_result)
            self.log.debug("Obtained valid full module name %s", mod_name)
        return mod_name

    def det_install_subdir(self, ec):
        """Determine name of software installation subdirectory."""
        self.log.debug("Determining software installation subdir for %s", ec)
        if build_option('fixed_installdir_naming_scheme'):
            subdir = os.path.join(ec['name'], det_full_ec_version(ec))
            self.log.debug("Using fixed naming software installation subdir: %s", subdir)
        else:
            subdir = self.mns.det_install_subdir(self.check_ec_type(ec))
            self.log.debug("Obtained subdir %s", subdir)
        return subdir

    def det_devel_module_filename(self, ec, force_visible=False):
        """Determine devel module filename."""
        modname = self.det_full_module_name(ec, force_visible=force_visible)
        return modname.replace(os.path.sep, '-') + DEVEL_MODULE_SUFFIX

    def det_short_module_name(self, ec, force_visible=False):
        """Determine short module name according to module naming scheme."""
        self.log.debug("Determining short module name for %s (force_visible: %s)" % (ec, force_visible))
        mod_name = self._det_module_name_with(self.mns.det_short_module_name, ec, force_visible=force_visible)
        self.log.debug("Obtained valid short module name %s" % mod_name)

        # sanity check: obtained module name should pass the 'is_short_modname_for' check
        if 'modaltsoftname' in ec and not self.is_short_modname_for(mod_name, ec['modaltsoftname'] or ec['name']):
            raise EasyBuildError("is_short_modname_for('%s', '%s') for active module naming scheme returns False",
                                 mod_name, ec['name'])
        return mod_name

    def det_module_subdir(self, ec):
        """Determine module subdirectory according to module naming scheme."""
        self.log.debug("Determining module subdir for %s" % ec)
        mod_subdir = self.mns.det_module_subdir(self.check_ec_type(ec))
        self.log.debug("Obtained subdir %s" % mod_subdir)
        return mod_subdir

    def det_module_symlink_paths(self, ec):
        """
        Determine list of paths in which symlinks to module files must be created.
        """
        return self.mns.det_module_symlink_paths(ec)

    def det_modpath_extensions(self, ec):
        """Determine modulepath extensions according to module naming scheme."""
        self.log.debug("Determining modulepath extensions for %s" % ec)
        modpath_extensions = self.mns.det_modpath_extensions(self.check_ec_type(ec))
        self.log.debug("Obtained modulepath extensions: %s" % modpath_extensions)
        return modpath_extensions

    def det_user_modpath_extensions(self, ec):
        """Determine user-specific modulepath extensions according to module naming scheme."""
        self.log.debug("Determining user modulepath extensions for %s", ec)
        modpath_extensions = self.mns.det_user_modpath_extensions(self.check_ec_type(ec))
        self.log.debug("Obtained user modulepath extensions: %s", modpath_extensions)
        return modpath_extensions

    def det_init_modulepaths(self, ec):
        """Determine initial modulepaths according to module naming scheme."""
        self.log.debug("Determining initial module paths for %s" % ec)
        init_modpaths = self.mns.det_init_modulepaths(self.check_ec_type(ec))
        self.log.debug("Obtained initial module paths: %s" % init_modpaths)
        return init_modpaths

    def expand_toolchain_load(self, ec=None):
        """
        Determine whether load statements for a toolchain should be expanded to load statements for its dependencies.
        This is useful when toolchains are not exposed to users.
        """
        return self.mns.expand_toolchain_load(ec=ec)

    def is_short_modname_for(self, short_modname, name):
        """
        Determine whether the specified (short) module name is a module for software with the specified name.
        """
        return self.mns.is_short_modname_for(short_modname, name)
