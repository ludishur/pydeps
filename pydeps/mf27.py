"""Find modules used by a script, using introspection."""
# This module should be kept compatible with Python 2.2, see PEP 291.
from __future__ import print_function
from __future__ import generators

import dis
import imp
import marshal
import os
import sys
import types
import struct

READ_MODE = "r"

LOAD_CONST = chr(dis.opname.index('LOAD_CONST'))
IMPORT_NAME = chr(dis.opname.index('IMPORT_NAME'))
STORE_NAME = chr(dis.opname.index('STORE_NAME'))
STORE_GLOBAL = chr(dis.opname.index('STORE_GLOBAL'))
STORE_OPS = [STORE_NAME, STORE_GLOBAL]
HAVE_ARGUMENT = chr(dis.HAVE_ARGUMENT)

# Modulefinder does a good job at simulating Python's, but it can not
# handle __path__ modifications packages make at runtime.  Therefore there
# is a mechanism whereby you can register extra paths in this map for a
# package, and it will be honored.

# Note this is a mapping is lists of paths.
packagePathMap = {}


# A Public interface
def AddPackagePath(packagename, path):  # pragma: nocover
    paths = packagePathMap.get(packagename, [])
    paths.append(path)
    packagePathMap[packagename] = paths


replacePackageMap = {}


# This ReplacePackage mechanism allows modulefinder to work around the
# way the _xmlplus package injects itself under the name "xml" into
# sys.modules at runtime by calling ReplacePackage("_xmlplus", "xml")
# before running ModuleFinder.

def ReplacePackage(oldname, newname):
    replacePackageMap[oldname] = newname


class Module:  # pragma: nocover
    def __init__(self, name, file=None, path=None):
        self.__name__ = name
        self.__file__ = file
        self.__path__ = path
        self.__code__ = None
        # The set of global names that are assigned to in the module.
        # This includes those names imported through starimports of
        # Python modules.
        self.globalnames = {}
        # The set of starimports this module did that could not be
        # resolved, ie. a starimport from a non-Python module.
        self.starimports = {}

    def __repr__(self):
        s = "Module(%r" % (self.__name__,)
        if self.__file__ is not None:
            s = s + ", %r" % (self.__file__,)
        if self.__path__ is not None:
            s = s + ", %r" % (self.__path__,)
        s = s + ")"
        return s


class ModuleFinder:
    def __init__(self, path=None, debug=0, excludes=[], replace_paths=[]):
        if path is None:
            path = sys.path
        self.path = path
        self.modules = {}
        self.badmodules = {}
        self.debug = debug
        self.indent = 0
        self.excludes = excludes
        self.replace_paths = replace_paths
        self.processed_paths = []  # Used in debugging only

    def msg(self, level, msgtxt, *args):  # pragma: nocover
        if level <= self.debug:
            for _i in range(self.indent):
                print("   ", end=' ')
            print(msgtxt, end=' ')
            for arg in args:
                print(repr(arg), end=' ')
            print()

    def msgin(self, *args):  # pragma: nocover
        level = args[0]
        if level <= self.debug:
            self.indent = self.indent + 1
            self.msg(*args)

    def msgout(self, *args):  # pragma: nocover
        level = args[0]
        if level <= self.debug:
            self.indent = self.indent - 1
            self.msg(*args)

    def run_script(self, pathname):
        self.msg(2, "run_script", pathname)
        with open(pathname, READ_MODE) as fp:
            stuff = ("", "r", imp.PY_SOURCE)
            self.load_module('__main__', fp, pathname, stuff)

    def load_file(self, pathname):
        _dir, name = os.path.split(pathname)
        name, ext = os.path.splitext(name)
        with open(pathname, READ_MODE) as fp:
            stuff = (ext, "r", imp.PY_SOURCE)
            self.load_module(name, fp, pathname, stuff)

    def import_hook(self, name, caller=None, fromlist=None, level=-1):
        self.msg(3, "import_hook: name(%s) caller(%s) fromlist(%s) level(%s)" % (name, caller, fromlist, level))
        parent = self.determine_parent(caller, level=level)
        # print "  import_hook parent:", parent
        q, tail = self.find_head_package(parent, name)
        if q.shortname in ('__future__', 'future'):  # the future package causes recursion overflow
            return None
        # print "  q:", q, "tail:", tail
        m = self.load_tail(q, tail)
        # print "  m:", m
        if not fromlist:
            return q
        if m.__path__:
            # print "  ensuring fromlist, m:", m, "fromlist:", fromlist
            self.ensure_fromlist(m, fromlist)
        return None

    def determine_parent(self, caller, level=-1):
        self.msgin(4, "determine_parent", caller, level)
        if not caller or level == 0:
            self.msgout(4, "determine_parent -> None")
            return None
        pname = caller.__name__
        if level >= 1:  # relative import
            if caller.__path__:
                level -= 1
            if level == 0:
                parent = self.modules[pname]
                assert parent is caller
                self.msgout(4, "determine_parent ->", parent)
                return parent
            if pname.count(".") < level:
                raise ImportError("relative importpath too deep")
            pname = ".".join(pname.split(".")[:-level])
            parent = self.modules[pname]
            self.msgout(4, "determine_parent ->", parent)
            return parent
        if caller.__path__:
            parent = self.modules[pname]
            assert caller is parent
            self.msgout(4, "determine_parent ->", parent)
            return parent
        if '.' in pname:
            i = pname.rfind('.')
            pname = pname[:i]
            parent = self.modules[pname]
            assert parent.__name__ == pname
            self.msgout(4, "determine_parent ->", parent)
            return parent
        self.msgout(4, "determine_parent -> None")
        return None

    def find_head_package(self, parent, name):
        self.msgin(4, "find_head_package", parent, name)
        if '.' in name:
            i = name.find('.')
            head = name[:i]
            tail = name[i + 1:]
        else:
            head = name
            tail = ""
        if parent:
            qname = "%s.%s" % (parent.__name__, head)
        else:
            qname = head
        q = self.import_module(head, qname, parent)
        if q:
            self.msgout(4, "find_head_package ->", (q, tail))
            return q, tail
        if parent:
            qname = head
            parent = None
            q = self.import_module(head, qname, parent)
            if q:
                self.msgout(4, "find_head_package ->", (q, tail))
                return q, tail
        self.msgout(4, "raise ImportError: No module named", qname)
        raise ImportError("No module named " + qname)

    def load_tail(self, q, tail):
        self.msgin(4, "load_tail", q, tail)
        m = q
        while tail:
            i = tail.find('.')
            if i < 0: i = len(tail)     # noqa
            head, tail = tail[:i], tail[i + 1:]
            mname = "%s.%s" % (m.__name__, head)
            m = self.import_module(head, mname, m)
            if not m:
                self.msgout(4, "raise ImportError: No module named", mname)
                raise ImportError("No module named " + mname)
        self.msgout(4, "load_tail ->", m)
        return m

    def ensure_fromlist(self, module, fromlist, recursive=0):  # pragma: nocover
        self.msg(4, "ensure_fromlist", module, fromlist, recursive)
        for sub in fromlist:
            if sub == "*":
                if not recursive:
                    submodules = self.find_all_submodules(module)
                    if submodules:
                        self.ensure_fromlist(module, submodules, 1)
            elif not hasattr(module, sub):
                subname = "%s.%s" % (module.__name__, sub)
                submod = self.import_module(sub, subname, module)
                if not submod:
                    raise ImportError("No module named " + subname)

    def find_all_submodules(self, m):
        if not m.__path__:
            return
        modules = {}
        # 'suffixes' used to be a list hardcoded to [".py", ".pyc", ".pyo"].
        # But we must also collect Python extension modules - although
        # we cannot separate normal dlls from Python extensions.
        suffixes = []
        for triple in imp.get_suffixes():
            suffixes.append(triple[0])
        for dirname in m.__path__:
            try:
                names = os.listdir(dirname)
            except os.error:
                self.msg(2, "can't list directory", dirname)
                continue
            for name in names:
                mod = None
                for suff in suffixes:
                    n = len(suff)
                    if name[-n:] == suff:
                        mod = name[:-n]
                        break
                if mod and mod != "__init__":
                    modules[mod] = mod
        return list(modules.keys())

    def import_module(self, partname, fqname, parent):
        self.msgin(3, "import_module: partname(%s) fqname(%s) parent(%s)" % (partname, fqname, parent))
        try:
            m = self.modules[fqname]
        except KeyError:
            pass
        else:
            self.msgout(3, "import_module2 ->", m)
            return m
        if fqname in self.badmodules:
            self.msgout(3, "import_module3 -> None")
            return None
        if parent and parent.__path__ is None:
            self.msgout(3, "import_module -> None")
            return None
        try:
            fp, pathname, stuff = self.find_module(partname,
                                                   parent and parent.__path__, parent)
        except ImportError:
            self.msgout(3, "import_module ->", None)
            return None
        try:
            m = self.load_module(fqname, fp, pathname, stuff)
        finally:
            if fp: fp.close()       # noqa
        if parent:
            setattr(parent, partname, m)
        self.msgout(3, "import_module ->", m)
        return m

    def load_module(self, fqname, fp, pathname, file_info):
        # fqname = dotted module name we're loading
        suffix, mode, kind = file_info
        kstr = {
            imp.PKG_DIRECTORY: 'PKG_DIRECTORY',
            imp.PY_SOURCE: 'PY_SOURCE',
            imp.PY_COMPILED: 'PY_COMPILED',
        }.get(kind, 'unknown-kind')
        self.msgin(2, "load_module(%s) fqname=%s, fp=%s, pathname=%s" % (kstr, fqname, fp and "fp", pathname))

        if kind == imp.PKG_DIRECTORY:
            module = self.load_package(fqname, pathname)
            self.msgout(2, "load_module ->", module)
            return module

        if kind == imp.PY_SOURCE:
            co = compile(
                fp.read() + '\n',
                pathname,
                'exec',            # compile code block
                dont_inherit=True  # don't inherit future statements from current environment
            )

        elif kind == imp.PY_COMPILED:
            # a .pyc file is a binary file containing only thee things:
            #  1. a four-byte magic number
            #  2. a four byte modification timestamp, and
            #  3. a Marshalled code object
            # from: https://nedbatchelder.com/blog/200804/the_structure_of_pyc_files.html
            if fp.read(4) != imp.get_magic():
                self.msgout(2, "raise ImportError: Bad magic number", pathname)
                raise ImportError("Bad magic number in %s" % pathname)
            fp.read(4)   # skip modification timestamp
            co = marshal.load(fp)  # load marshalled code object.

        else:
            co = None

        module = self.add_module(fqname)
        module.__file__ = pathname
        if co:
            if self.replace_paths:
                co = self.replace_paths_in_code(co)
            module.__code__ = co
            self.scan_code(co, module)
        self.msgout(2, "load_module ->", module)
        return module

    def _add_badmodule(self, name, caller):
        if name not in self.badmodules:
            self.badmodules[name] = {}
        if caller:
            self.badmodules[name][caller.__name__] = 1
        else:
            self.badmodules[name]["-"] = 1

    def _safe_import_hook(self, name, caller, fromlist, level=-1):
        # wrapper for self.import_hook() that won't raise ImportError
        if name in self.badmodules:
            self._add_badmodule(name, caller)
            return
        try:
            self.import_hook(name, caller, level=level)
        except ImportError as msg:
            self.msg(2, "ImportError:", str(msg))
            self._add_badmodule(name, caller)
        else:
            if fromlist:
                for sub in fromlist:
                    if sub in self.badmodules:
                        self._add_badmodule(sub, caller)
                        continue
                    try:
                        # print "    self.import_hook(name=%s, caller=%s, [sub]=%s, level=%s)" % (name, caller, [sub], level)
                        self.import_hook(name, caller, [sub], level=level)
                    except ImportError as msg:
                        self.msg(2, "ImportError:", str(msg))
                        fullname = name + "." + sub
                        self._add_badmodule(fullname, caller)

    def scan_opcodes(self, co,
                     unpack=struct.unpack):  # pragma: nocover
        # Scan the code, and yield 'interesting' opcode combinations
        # Version for Python 2.4 and older
        code = co.co_code
        names = co.co_names
        consts = co.co_consts
        while code:
            c = code[0]
            if c in STORE_OPS:
                oparg, = unpack('<H', code[1:3])
                yield "store", (names[oparg],)
                code = code[3:]
                continue
            if c == LOAD_CONST and code[3] == IMPORT_NAME:
                oparg_1, oparg_2 = unpack('<xHxH', code[:6])
                yield "import", (consts[oparg_1], names[oparg_2])
                code = code[6:]
                continue
            if c >= HAVE_ARGUMENT:
                code = code[3:]
            else:
                code = code[1:]

    def scan_opcodes_25(self, co,
                        unpack=struct.unpack):
        # Scan the code, and yield 'interesting' opcode combinations
        # Python 2.5 version (has absolute and relative imports)
        code = co.co_code
        names = co.co_names
        consts = co.co_consts
        LOAD_LOAD_AND_IMPORT = LOAD_CONST + LOAD_CONST + IMPORT_NAME
        while code:
            c = code[0]
            if c in STORE_OPS:
                oparg, = unpack('<H', code[1:3])
                yield "store", (names[oparg],)
                code = code[3:]
                continue
            if code[:9:3] == LOAD_LOAD_AND_IMPORT:
                oparg_1, oparg_2, oparg_3 = unpack('<xHxHxH', code[:9])
                level = consts[oparg_1]
                if level == -1:  # normal import
                    # print "import", (consts[oparg_2], names[oparg_3])
                    yield "import", (consts[oparg_2], names[oparg_3])
                elif level == 0:  # absolute import
                    # print "absolute_import", (consts[oparg_2], names[oparg_3])
                    yield "absolute_import", (consts[oparg_2], names[oparg_3])
                else:  # relative import
                    # print "relative_import", (level, consts[oparg_2], names[oparg_3])
                    yield "relative_import", (level, consts[oparg_2], names[oparg_3])
                code = code[9:]
                continue
            if c >= HAVE_ARGUMENT:
                code = code[3:]
            else:
                code = code[1:]

    def scan_opcodes_34(self, co):
        i = 0
        bytecode = list(dis.Bytecode(co))
        while i < len(bytecode):
            if (
                bytecode[i].opname == "LOAD_CONST" and
                bytecode[i + 1].opname == "LOAD_CONST" and
                bytecode[i + 2].opname == "IMPORT_NAME"
            ):
                level = bytecode[i].argval
                fromlist = bytecode[i + 1].argval
                import_name = bytecode[i + 2].argval
                if level == 0:
                    yield "absolute_import", (fromlist, import_name)
                else:
                    yield "relative_import", (level, fromlist, import_name)
                i += 2
            i += 1

    def scan_code(self, co, module):
        # code = co.co_code
        if sys.version_info >= (3, 4):
            scanner = self.scan_opcodes_34
        elif sys.version_info >= (2, 5):
            scanner = self.scan_opcodes_25
        else:
            scanner = self.scan_opcodes
        for what, args in scanner(co):
            # print "WHAT:", what, args
            if what == "store":
                name, = args
                module.globalnames[name] = 1
            elif what in ("import", "absolute_import"):
                fromlist, name = args
                have_star = 0
                if fromlist is not None:
                    if "*" in fromlist:
                        have_star = 1
                    fromlist = [f for f in fromlist if f != "*"]
                if what == "absolute_import":
                    level = 0
                else:
                    level = -1
                self._safe_import_hook(name, module, fromlist, level=level)
                if have_star:
                    # We've encountered an "import *". If it is a Python module,
                    # the code has already been parsed and we can suck out the
                    # global names.
                    mm = None
                    if module.__path__:
                        # At this point we don't know whether 'name' is a
                        # submodule of 'module' or a global module. Let's just try
                        # the full name first.
                        mm = self.modules.get(module.__name__ + "." + name)
                    if mm is None:
                        mm = self.modules.get(name)
                    if mm is not None:
                        module.globalnames.update(mm.globalnames)
                        module.starimports.update(mm.starimports)
                        if mm.__code__ is None:
                            module.starimports[name] = 1
                    else:
                        module.starimports[name] = 1
            elif what == "relative_import":
                level, fromlist, name = args
                if name:
                    self._safe_import_hook(name, module, fromlist, level=level)
                else:
                    parent = self.determine_parent(module, level=level)
                    # print "  NAME:", parent
                    # print "  CALLER:", module
                    # print "  FROMLIST:", fromlist
                    # print "  LEVEL:", 0
                    # m is still the caller here... [bp]
                    self._safe_import_hook(parent.__name__, module, fromlist, level=0)
                    # self._safe_import_hook(parent.__name__, None, fromlist, level=0)
            else:
                # We don't expect anything else from the generator.
                raise RuntimeError(what)

        for literal in co.co_consts:
            if isinstance(literal, type(co)):
                self.scan_code(literal, module)

    def load_package(self, fqname, pathname):
        self.msgin(2, "load_package", fqname, pathname)
        newname = replacePackageMap.get(fqname)
        if newname:
            fqname = newname
        m = self.add_module(fqname)
        m.__file__ = pathname
        m.__path__ = [pathname]

        # As per comment at top of file, simulate runtime __path__ additions.
        m.__path__ = m.__path__ + packagePathMap.get(fqname, [])

        fp, buf, stuff = self.find_module("__init__", m.__path__)
        self.load_module(fqname, fp, buf, stuff)
        self.msgout(2, "load_package ->", m)
        return m

    def add_module(self, fqname):  # pragma: nocover
        if fqname in self.modules:
            return self.modules[fqname]
        self.modules[fqname] = m = Module(fqname)
        return m

    def find_module(self, name, path, parent=None):
        if parent is not None:
            # assert path is not None
            fullname = parent.__name__ + '.' + name
        else:
            fullname = name
        if fullname in self.excludes:
            self.msgout(3, "find_module -> Excluded", fullname)
            raise ImportError(name)

        if path is None:
            if name in sys.builtin_module_names:
                return (None, None, ("", "", imp.C_BUILTIN))

            path = self.path
        return imp.find_module(name, path)

    def report(self):  # pragma: nocover
        """Print a report to stdout, listing the found modules with their
        paths, as well as modules that are missing, or seem to be missing.
        """
        print()
        print("  %-25s %s" % ("Name", "File"))
        print("  %-25s %s" % ("----", "----"))
        # Print modules found
        keys = list(self.modules.keys())
        keys.sort()
        for key in keys:
            m = self.modules[key]
            if m.__path__:
                print("P", end=' ')
            else:
                print("m", end=' ')
            print("%-25s" % key, m.__file__ or "")

        # Print missing modules
        missing, maybe = self.any_missing_maybe()
        if missing:
            print()
            print("Missing modules:")
            for name in missing:
                mods = list(self.badmodules[name].keys())
                mods.sort()
                print("?", name, "imported from", ', '.join(mods))
        # Print modules that may be missing, but then again, maybe not...
        if maybe:
            print()
            print("Submodules thay appear to be missing, but could also be", end=' ')
            print("global names in the parent package:")
            for name in maybe:
                mods = list(self.badmodules[name].keys())
                mods.sort()
                print("?", name, "imported from", ', '.join(mods))

    def any_missing(self):
        """Return a list of modules that appear to be missing. Use
        any_missing_maybe() if you want to know which modules are
        certain to be missing, and which *may* be missing.
        """
        missing, maybe = self.any_missing_maybe()
        return missing + maybe

    def any_missing_maybe(self):  # pragma: nocover
        """Return two lists, one with modules that are certainly missing
        and one with modules that *may* be missing. The latter names could
        either be submodules *or* just global names in the package.

        The reason it can't always be determined is that it's impossible to
        tell which names are imported when ``from module import *`` is done
        with an extension module, short of actually importing it.
        """
        missing = []
        maybe = []
        for name in self.badmodules:
            if name in self.excludes:
                continue
            i = name.rfind(".")
            if i < 0:
                missing.append(name)
                continue
            subname = name[i + 1:]
            pkgname = name[:i]
            pkg = self.modules.get(pkgname)
            if pkg is not None:
                if pkgname in self.badmodules[name]:
                    # The package tried to import this module itself and
                    # failed. It's definitely missing.
                    missing.append(name)
                elif subname in pkg.globalnames:
                    # It's a global in the package: definitely not missing.
                    pass
                elif pkg.starimports:
                    # It could be missing, but the package did an "import *"
                    # from a non-Python module, so we simply can't be sure.
                    maybe.append(name)
                else:
                    # It's not a global in the package, the package didn't
                    # do funny star imports, it's very likely to be missing.
                    # The symbol could be inserted into the package from the
                    # outside, but since that's not good style we simply list
                    # it missing.
                    missing.append(name)
            else:
                missing.append(name)
        missing.sort()
        maybe.sort()
        return missing, maybe

    def replace_paths_in_code(self, co):
        new_filename = original_filename = os.path.normpath(co.co_filename)
        for f, r in self.replace_paths:
            if original_filename.startswith(f):
                new_filename = r + original_filename[len(f):]
                break

        if self.debug and original_filename not in self.processed_paths:
            if new_filename != original_filename:
                self.msgout(2, "co_filename %r changed to %r"
                            % (original_filename, new_filename,))
            else:
                self.msgout(2, "co_filename %r remains unchanged"
                            % (original_filename,))
            self.processed_paths.append(original_filename)

        consts = list(co.co_consts)
        for i in range(len(consts)):
            if isinstance(consts[i], type(co)):
                consts[i] = self.replace_paths_in_code(consts[i])

        return types.CodeType(co.co_argcount, co.co_nlocals, co.co_stacksize,
                              co.co_flags, co.co_code, tuple(consts), co.co_names,
                              co.co_varnames, new_filename, co.co_name,
                              co.co_firstlineno, co.co_lnotab,
                              co.co_freevars, co.co_cellvars)


def test():  # pragma: nocover
    # Parse command line
    import getopt

    try:
        opts, args = getopt.getopt(sys.argv[1:], "dmp:qx:")
    except getopt.error as msg:
        print(msg)
        return

    # Process options
    debug = 1
    domods = 0
    addpath = []
    exclude = []
    for o, a in opts:
        if o == '-d':
            debug = debug + 1
        if o == '-m':
            domods = 1
        if o == '-p':
            addpath = addpath + a.split(os.pathsep)
        if o == '-q':
            debug = 0
        if o == '-x':
            exclude.append(a)

    # Provide default arguments
    if not args:
        script = "hello.py"
    else:
        script = args[0]

    # Set the path based on sys.path and the script directory
    path = sys.path[:]
    path[0] = os.path.dirname(script)
    path = addpath + path
    if debug > 1:
        print("path:")
        for item in path:
            print("   ", repr(item))

    # Create the module finder and turn its crank
    mf = ModuleFinder(path, debug, exclude)
    for arg in args[1:]:
        if arg == '-m':
            domods = 1
            continue
        if domods:
            if arg[-2:] == '.*':
                mf.import_hook(arg[:-2], None, ["*"])
            else:
                mf.import_hook(arg)
        else:
            mf.load_file(arg)
    mf.run_script(script)
    mf.report()
    return mf  # for -i debugging


if __name__ == '__main__':  # pragma: nocover
    try:
        mf = test()
    except KeyboardInterrupt:
        print("\n[interrupt]")
