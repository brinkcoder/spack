# Copyright 2013-2023 Lawrence Livermore National Security, LLC and other
# Spack Project Developers. See the top-level COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import os
import stat as st

import llnl.util.filesystem as fs

from spack.util.error import UtilityError


def set_permissions(path, perms, group=None):
    # Preserve higher-order bits of file permissions
    perms |= os.stat(path).st_mode & (st.S_ISUID | st.S_ISGID | st.S_ISVTX)

    # Do not let users create world/group writable suid binaries
    if perms & st.S_ISUID:
        if perms & st.S_IWOTH:
            raise InvalidPermissionsError("Attempting to set suid with world writable")
        if perms & st.S_IWGRP:
            raise InvalidPermissionsError("Attempting to set suid with group writable")
    # Or world writable sgid binaries
    if perms & st.S_ISGID:
        if perms & st.S_IWOTH:
            raise InvalidPermissionsError("Attempting to set sgid with world writable")

    fs.chmod_x(path, perms)

    if group:
        fs.chgrp(path, group, follow_symlinks=False)


class InvalidPermissionsError(UtilityError):
    """Error class for invalid permission setters"""
