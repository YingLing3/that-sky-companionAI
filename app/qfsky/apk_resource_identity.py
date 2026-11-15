"""Pure runtime check of derived asset provenance, without APK or account I/O."""
from __future__ import annotations

import re

from .version_profile import profile_for_native_sha256

_RESOURCE_KEYS = ('resource_identity_schema', 'resource_identity_scope',
                  'resource_identity_sha256', 'reference_apk_archive_directory_sha256')


def resource_provenance_matches(provenance, *, native_sha256,
                                reference_directory_sha256, scope='catalog'):
    """Accept exact legacy evidence or a complete, pinned resource-subset proof.

    A partial/incorrect new proof never falls back to the legacy branch. Actual
    archive identity stays an observation; it is not replaced by the reference.
    Scene/member/actor/account pins remain the callers' independent checks.
    """
    if type(provenance) is not dict or provenance.get('native_sha256') != native_sha256:
        return False
    actual = provenance.get('apk_archive_directory_sha256')
    if type(actual) is not str or re.fullmatch(r'[0-9a-f]{64}', actual) is None:
        return False
    if not any(key in provenance for key in _RESOURCE_KEYS):
        return actual == reference_directory_sha256
    profile = profile_for_native_sha256(native_sha256)
    if profile is None or profile.get('apk_directory_sha256') != reference_directory_sha256:
        return False
    expected = profile.get('resource_identity_sha256', {}).get(scope)
    if type(expected) is not str or re.fullmatch(r'[0-9a-f]{64}', expected) is None:
        return False
    if ('apk_build' in provenance and
            (type(provenance['apk_build']) is not int or provenance['apk_build'] != profile['build'])):
        return False
    return (
        provenance.get('resource_identity_schema') == profile.get('resource_identity_schema') == 'xg-apk-resource-members-v1'
        and provenance.get('resource_identity_scope') == scope
        and provenance.get('resource_identity_sha256') == expected
        and provenance.get('reference_apk_archive_directory_sha256') == reference_directory_sha256
    )
