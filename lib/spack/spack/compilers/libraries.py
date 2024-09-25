# Copyright 2013-2023 Lawrence Livermore National Security, LLC and other
# Spack Project Developers. See the top-level COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
import contextlib
import os
import re
import shutil
import tempfile
import typing
from typing import List, Optional

import llnl.path
from llnl.util import tty
from llnl.util.filesystem import path_contains_subdirectory, paths_containing_libs

import spack.util.libc
from spack.util.environment import filter_system_paths

if typing.TYPE_CHECKING:
    import spack.spec


#: regex for parsing linker lines
_LINKER_LINE = re.compile(r"^( *|.*[/\\])" r"(link|ld|([^/\\]+-)?ld|collect2)" r"[^/\\]*( |$)")

#: components of linker lines to ignore
_LINKER_LINE_IGNORE = re.compile(r"(collect2 version|^[A-Za-z0-9_]+=|/ldfe )")

#: regex to match linker search paths
_LINK_DIR_ARG = re.compile(r"^-L(.:)?(?P<dir>[/\\].*)")

#: regex to match linker library path arguments
_LIBPATH_ARG = re.compile(r"^[-/](LIBPATH|libpath):(?P<dir>.*)")


@llnl.path.system_path_filter
def parse_non_system_link_dirs(compiler_debug_output: str) -> List[str]:
    """Parses link paths out of compiler debug output.

    Args:
        compiler_debug_output: compiler debug output as a string

    Returns:
        Implicit link paths parsed from the compiler output
    """
    link_dirs = _parse_link_paths(compiler_debug_output)

    # Remove directories that do not exist. Some versions of the Cray compiler
    # report nonexistent directories
    link_dirs = filter_non_existing_dirs(link_dirs)

    # Return set of directories containing needed compiler libs, minus
    # system paths. Note that 'filter_system_paths' only checks for an
    # exact match, while 'in_system_subdirectory' checks if a path contains
    # a system directory as a subdirectory
    link_dirs = filter_system_paths(link_dirs)
    return list(p for p in link_dirs if not in_system_subdirectory(p))


def filter_non_existing_dirs(dirs):
    return [d for d in dirs if os.path.isdir(d)]


def in_system_subdirectory(path):
    system_dirs = [
        "/lib/",
        "/lib64/",
        "/usr/lib/",
        "/usr/lib64/",
        "/usr/local/lib/",
        "/usr/local/lib64/",
    ]
    return any(path_contains_subdirectory(path, x) for x in system_dirs)


def _parse_link_paths(string):
    """Parse implicit link paths from compiler debug output.

    This gives the compiler runtime library paths that we need to add to
    the RPATH of generated binaries and libraries.  It allows us to
    ensure, e.g., that codes load the right libstdc++ for their compiler.
    """
    lib_search_paths = False
    raw_link_dirs = []
    for line in string.splitlines():
        if lib_search_paths:
            if line.startswith("\t"):
                raw_link_dirs.append(line[1:])
                continue
            else:
                lib_search_paths = False
        elif line.startswith("Library search paths:"):
            lib_search_paths = True

        if not _LINKER_LINE.match(line):
            continue
        if _LINKER_LINE_IGNORE.match(line):
            continue
        tty.debug(f"implicit link dirs: link line: {line}")

        next_arg = False
        for arg in line.split():
            if arg in ("-L", "-Y"):
                next_arg = True
                continue

            if next_arg:
                raw_link_dirs.append(arg)
                next_arg = False
                continue

            link_dir_arg = _LINK_DIR_ARG.match(arg)
            if link_dir_arg:
                link_dir = link_dir_arg.group("dir")
                raw_link_dirs.append(link_dir)

            link_dir_arg = _LIBPATH_ARG.match(arg)
            if link_dir_arg:
                link_dir = link_dir_arg.group("dir")
                raw_link_dirs.append(link_dir)

    implicit_link_dirs = list()
    visited = set()
    for link_dir in raw_link_dirs:
        normalized_path = os.path.abspath(link_dir)
        if normalized_path not in visited:
            implicit_link_dirs.append(normalized_path)
            visited.add(normalized_path)

    tty.debug(f"implicit link dirs: result: {', '.join(implicit_link_dirs)}")
    return implicit_link_dirs


class CompilerPropertyDetector:

    _CACHE = {}

    def __init__(self, compiler_spec: "spack.spec.Spec"):
        assert compiler_spec.external, "only external compiler specs are allowed, so far"
        assert compiler_spec.concrete, "only concrete compiler specs are allowed, so far"
        self.spec = compiler_spec

    @contextlib.contextmanager
    def compiler_environment(self):
        """Sets the environment to run this compiler"""
        import spack.schema.environment
        import spack.util.module_cmd

        # Avoid modifying os.environ if possible.
        environment = self.spec.extra_attributes.get("environment", {})
        modules = self.spec.external_modules or []
        if not self.spec.external_modules and not environment:
            yield
            return

        # store environment to replace later
        backup_env = os.environ.copy()

        try:
            # load modules and set env variables
            for module in modules:
                spack.util.module_cmd.load_module(module)

            # apply other compiler environment changes
            spack.schema.environment.parse(environment).apply_modifications()

            yield
        finally:
            # Restore environment regardless of whether inner code succeeded
            os.environ.clear()
            os.environ.update(backup_env)

    def _compile_dummy_c_source(self) -> Optional[str]:
        import spack.util.executable

        assert self.spec.external, "only external compiler specs are allowed, so far"
        compiler_pkg = self.spec.package
        cc = compiler_pkg.cc if compiler_pkg.cc else compiler_pkg.cxx
        if not cc:  # or not self.spec.verbose_flag:
            return None

        try:
            tmpdir = tempfile.mkdtemp(prefix="spack-implicit-link-info")
            fout = os.path.join(tmpdir, "output")
            fin = os.path.join(tmpdir, "main.c")

            with open(fin, "w") as csource:
                csource.write(
                    "int main(int argc, char* argv[]) { (void)argc; (void)argv; return 0; }\n"
                )
            cc_exe = spack.util.executable.Executable(cc)

            # FIXME (compiler as nodes): this operation should be encapsulated somewhere else
            compiler_flags = self.spec.extra_attributes.get("flags", {})
            for flag_type in [
                "cflags" if cc == compiler_pkg.cc else "cxxflags",
                "cppflags",
                "ldflags",
            ]:
                current_flags = compiler_flags.get(flag_type, "").strip()
                if current_flags:
                    cc_exe.add_default_arg(*current_flags.split(" "))

            with self.compiler_environment():
                return cc_exe("-v", fin, "-o", fout, output=str, error=str)
        except spack.util.executable.ProcessError as pe:
            tty.debug(f"ProcessError: Command exited with non-zero status: {pe.long_message}")
            return None
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def compiler_verbose_output(self) -> Optional[str]:
        key = self.spec.dag_hash()
        if key not in self._CACHE:
            self._CACHE[key] = self._compile_dummy_c_source()
        return self._CACHE[key]

    def default_libc(self) -> Optional["spack.spec.Spec"]:
        """Determine libc targeted by the compiler from link line"""
        output = self.compiler_verbose_output()

        if not output:
            return None

        dynamic_linker = spack.util.libc.parse_dynamic_linker(output)

        if not dynamic_linker:
            return None

        return spack.util.libc.libc_from_dynamic_linker(dynamic_linker)

    def implicit_rpaths(self) -> List[str]:
        output = self.compiler_verbose_output
        if output is None:
            return []

        link_dirs = parse_non_system_link_dirs(output)
        all_required_libs = list(self.spec.package.required_libs) + ["libc", "libc++", "libstdc++"]
        return list(paths_containing_libs(link_dirs, all_required_libs))
