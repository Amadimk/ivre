#! /usr/bin/env python
# -*- coding: utf-8 -*-

# This file is part of IVRE.
# Copyright 2011 - 2020 Pierre LALET <pierre@droids-corp.org>
#
# IVRE is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# IVRE is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public
# License for more details.
#
# You should have received a copy of the GNU General Public License
# along with IVRE. If not, see <http://www.gnu.org/licenses/>.


import struct
import binascii


from ivre import utils


def _extract_substr(ntlm_msg, offset, ln, uses_unicode):
    """
    Extract the string at te given offset and of the given length from an
    NTLM message
    """
    s = ntlm_msg[offset:offset + ln]
    if len(s) < ln:
        utils.LOGGER.warning("Data too small at offset %s [%r, size %d]",
                             offset, ntlm_msg, ln)
        raise ValueError
    if uses_unicode:
        try:
            return utils.nmap_encode_data(s.decode('utf-16').encode('utf-8'))
        except UnicodeDecodeError:
            utils.LOGGER.warning("Cannot decode %r", s)
            return utils.nmap_encode_data(s)
    else:
        # Test whether the string is in UTF-16 encoding
        if set(s[1::2]) in [{0}, {b'\x00'}]:
            try:
                return utils.nmap_encode_data(
                    s.decode('utf-16').encode('utf-8')
                )
            except UnicodeDecodeError:
                pass
            else:
                utils.LOGGER.warning("NTLM message should use code page "
                                     "encoding but one of its fields (%r) is "
                                     "encoded in UTF-16", s)
        return utils.nmap_encode_data(s)


# The positions of `Negotiate Version` and `Negotiate Target Info`
# in the NTLM flags
flag_version = 0x2000000
flag_targetinfo = 0x800000
flag_unicode = 0x1
flag_oem = 0x2


# https://winprotocoldoc.blob.core.windows.net/productionwindowsarchives/MS-NLMP/%5bMS-NLMP%5d.pdf
# p34
def is_unicode(msg, flags):
    if flags & flag_unicode:
        return True
    if flags & flag_oem:
        return False
    utils.LOGGER.warning("NTLM message (%r) has no encoding specified", msg)
    return False


# Target info types :
#  - 1: NetBIOS Computer Name
#  - 2: NetBIOS Domain Name
#  - 3: DNS Computer Name
#  - 4: DNS Domain Name
#  - 5: DNS Tree Name
info_types = {1: 'NetBIOS_Computer_Name', 2: 'NetBIOS_Domain_Name',
              3: 'DNS_Computer_Name', 4: 'DNS_Domain_Name', 5: 'DNS_Tree_Name'}


def _ntlm_challenge_extract(challenge):
    """
    Extract host information in an NTLM_CHALLENGE message
    """
    if len(challenge) < 24:
        utils.LOGGER.warning("NTLM message is abnormally short [%r, size %d]",
                             challenge, len(challenge))
        return None

    value = {}
    flags = struct.unpack('I', challenge[20:24])[0]

    uses_unicode = is_unicode(challenge, flags)

    # Get target name
    lntarget, offset = struct.unpack('H2xH', challenge[12:18])
    try:
        value['Target_Name'] = _extract_substr(challenge, offset, lntarget,
                                               uses_unicode)
    except ValueError:
        pass

    # Multiple versions of NTLM Challenge messages exist (they can be deduced
    # thanks to the target offset)
    #   V1: No context, no target information and no OS version are provided
    #       - offset 32
    #   V2: Context and target informatio are provided but not the OS version
    #       - offset 48
    #   V3: The context, target information and OS Version are all provided
    #       - offset >= 56
    # cf http://davenport.sourceforge.net/ntlm.html#osVersionStructure

    # Get OS Version if the version of NTLM handles it
    # and the `Negotiate version` flag is set
    if offset >= 56 and flags & flag_version:
        if len(challenge) < 56:
            utils.LOGGER.warning("NTLM message should contain version info at "
                                 "offset 56 but is too short (size %d)",
                                 len(challenge))
            return value

        maj, minor, bld, ntlm_ver = struct.unpack('BBH3xB', challenge[48:56])
        try:
            value['Product_Version'] = "{}.{}.{}".format(maj, minor, bld)
        except ValueError:
            pass
        try:
            value['NTLM_Version'] = ntlm_ver
        except ValueError:
            pass

    # Get target information if the version of NTLM handles it
    # and the `Negotiate Target Info` is set
    if offset >= 48 and flags & flag_targetinfo:
        if len(challenge) < 46:
            utils.LOGGER.warning("NTLM message should contain target info at "
                                 "offset 48 but is too short (size %d)",
                                 len(challenge))
            return value

        ln_info, off = struct.unpack('HH', challenge[42:46])
        challenge = challenge[off:]
        # Return if the target info block is shorter than it is supposed to be
        if len(challenge) < ln_info:
            utils.LOGGER.warning("NTLM target info should be of size %d but "
                                 "is too short (size %d)", ln_info,
                                 len(challenge))
            return value

        while len(challenge) <= ln_info:
            typ, ln = struct.unpack('HH', challenge[0:4])
            if 1 <= typ <= 5:
                try:
                    value[info_types[typ]] = _extract_substr(challenge, 4, ln,
                                                             uses_unicode)
                except ValueError:
                    pass
                challenge = challenge[4 + ln:]
            else:
                return value

    return value


def _ntlm_authenticate_info(request):
    """
    Extract host information in an NTLM_AUTH message
    """
    if len(request) < 52:
        utils.LOGGER.warning("NTLM message is too short (%d) but should be "
                             "at least 52 char long", len(request))
        return None

    value = {}
    ln, offset = struct.unpack('H2xI', request[28:36])
    has_version = False
    # Flags are not present in an NTLM_AUTH message when the data block starts
    # before index 64
    flags = 0x0
    if offset >= 64 and request[64:]:
        flags, = struct.unpack('I', request[60:64])
        has_version = flags & flag_version

    uses_unicode = is_unicode(request, flags)
    if ln:
        try:
            value['NetBIOS_Domain_Name'] = _extract_substr(request, offset, ln,
                                                           uses_unicode)
        except ValueError:
            pass

    ln, off = struct.unpack('H2xI', request[36:44])
    if ln:
        try:
            value['User_Name'] = _extract_substr(request, off, ln,
                                                 uses_unicode)
        except ValueError:
            pass
    ln, off = struct.unpack('H2xI', request[44:52])
    if ln:
        try:
            value['Workstation'] = _extract_substr(request, off, ln,
                                                   uses_unicode)
        except ValueError:
            pass

    # Get OS Version if the `Negotiate Version` is set
    # (NTLM_AUTH messages with a data block starting before index 72 do not
    # contain information on the version)
    if has_version and offset >= 72 and request[72:]:
        maj, minor, bld, ntlm_ver = struct.unpack('BBH3xB', request[64:72])
        try:
            value['Product_Version'] = "{}.{}.{}".format(maj, minor, bld)
        except ValueError:
            pass
        try:
            value['NTLM_Version'] = ntlm_ver
        except ValueError:
            pass

    return value


def ntlm_extract_info(value):
    """
    Extract valuable host information from an NTLM message
    """
    ntlm_type, = struct.unpack('I', value[8:12])

    if ntlm_type == 2:
        return _ntlm_challenge_extract(value)

    if ntlm_type == 3:
        return _ntlm_authenticate_info(value)

    # NTLM_NEGOTIATE messages are not handled yet
    return {}


def _ntlm_dict2string(dic):
    """
    Returns a string with the keys and values (encoded in base64)
    of the given dict, in the format
    """
    return ','.join("{}:{}".format(k, (v if k == 'NTLM_Version'
                                       else utils.encode_b64(
                                           v.encode()).decode()))
                    for k, v in dic.items())


def _is_ntlm_message(message):
    """
    Checks whether the given string is an NTLM message
    """
    if message[:4].lower() == 'ntlm' and message[4:].strip():
        return True
    if message[:9].lower() == 'negotiate':
        message = message.split(None, 1)
        if message[1:]:
            try:
                return utils.decode_b64(message[1].encode())[:7] == b'NTLMSSP'
            except (UnicodeDecodeError, TypeError, ValueError, binascii.Error):
                pass
    return False
