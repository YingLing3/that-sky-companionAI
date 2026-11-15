# -*- coding: utf-8 -*-
"""Native build identity plus the exact APK members read by each generator.

The shipped manifest contains names, sizes and CRCs only, never APK payloads.
Signing/channel metadata and unrelated members do not participate in a current
build's resource identity. The old build has no verified subset manifest and
therefore remains exact-directory, recognition-only legacy support.
"""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path

from .common import GameDataError
from qfsky.version_profile import PROFILES, profile_for_native_sha256

SCHEMA = "xg-apk-resource-members-v1"
NATIVE_MEMBER = "lib/arm64-v8a/libBootloader.so"
CONSUMERS = ("names", "catalog", "terrain", "props")
MAX_DIFFERENCES = 8


def archive_directory_sha(zf, *, reject_duplicates=True):
    rows = [(info.filename, info.file_size, info.CRC) for info in zf.infolist()]
    if reject_duplicates and len(rows) != len({row[0] for row in rows}):
        raise GameDataError("APK 里有重复文件成员，拒绝使用。", "apk_duplicate_member")
    return hashlib.sha256(json.dumps(sorted(rows), separators=(",", ":")).encode("utf-8")).hexdigest()


def native_sha256(zf):
    matches = [info for info in zf.infolist() if info.filename == NATIVE_MEMBER]
    if not matches:
        raise GameDataError("安装包缺少 %s，请使用完整 base.apk。" % NATIVE_MEMBER, "apk_no_native")
    if len(matches) != 1:
        raise GameDataError("APK 里有重复的关键成员：%s。" % NATIVE_MEMBER, "apk_duplicate_member")
    digest = hashlib.sha256()
    with zf.open(matches[0]) as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _consumer_payload(manifest, consumer):
    policy = manifest["consumers"][consumer]
    return {
        "schema": SCHEMA,
        "build": manifest["build"],
        "native_sha256": manifest["native_sha256"],
        "scope": consumer,
        "members": [[name, manifest["members"][name]["size"], manifest["members"][name]["crc32"]]
                    for name in sorted(policy["members"])],
        "inventories": [{"label": row["label"], "pattern": row["pattern"],
                         "members": sorted(row["members"])}
                        for row in sorted(policy.get("inventories", []), key=lambda row: row["label"])],
    }


def consumer_digest(manifest, consumer):
    payload = _consumer_payload(manifest, consumer)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _load_manifest(build):
    path = Path(__file__).with_name("apk_members_%s.json" % build)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        profile = PROFILES[build]
        if (manifest["schema"] != SCHEMA or manifest["build"] != build
                or manifest["native_sha256"] != profile["native_sha256"]
                or set(manifest["consumers"]) != set(CONSUMERS)):
            raise ValueError("manifest identity")
        pins = profile.get("resource_identity_sha256") or {}
        for consumer in CONSUMERS:
            row = manifest["consumers"][consumer]
            if len(row["members"]) != len(set(row["members"])):
                raise ValueError("duplicate member declaration")
            for name in row["members"]:
                meta = manifest["members"][name]
                if (not isinstance(name, str) or type(meta["size"]) is not int or meta["size"] < 0
                        or type(meta["crc32"]) is not int or not 0 <= meta["crc32"] <= 0xFFFFFFFF):
                    raise ValueError("member metadata")
            if consumer_digest(manifest, consumer) != pins.get(consumer):
                raise ValueError("consumer pin")
        return manifest
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise GameDataError("本地 APK 资源成员基线缺失或损坏（%s）。" % build,
                            "apk_resource_manifest") from exc


def _validate_open(zf, consumers, expected_build):
    native = native_sha256(zf)
    profile = profile_for_native_sha256(native)
    if profile is None:
        raise GameDataError("libBootloader.so 的 SHA-256 不属于支持的国服 0.16.8 / 0.16.7 版本。",
                            "apk_identity_mismatch")
    build = profile["build"]
    if expected_build is not None and build != expected_build:
        raise GameDataError("安装包 native 版本为 %s / %s，生成器需要 build %s。"
                            % (profile["version"], build, expected_build), "apk_build_mismatch")
    directory = archive_directory_sha(zf, reject_duplicates=bool(profile.get("legacy")))
    result = {
        "build": build, "version": profile["version"], "native_sha256": native,
        "apk_archive_directory_sha256": directory,
        "reference_apk_archive_directory_sha256": profile["apk_directory_sha256"],
        "consumers": list(consumers), "legacy": bool(profile.get("legacy")),
    }
    if profile.get("legacy"):
        if directory != profile["apk_directory_sha256"]:
            raise GameDataError("0.16.7 尚无可验证的资源子集基线，仅识别原始精确目录档位。",
                                "apk_identity_mismatch")
        result.update(identity_mode="legacy-native-and-exact-directory", consumer_sha256={},
                      resource_identity_sha256=None, identity_digest=None, member_count=0)
        return result
    manifest = _load_manifest(build)
    infos = {}
    for info in zf.infolist():
        infos.setdefault(info.filename, []).append(info)
    selected, differences = set(), []
    for consumer in consumers:
        policy = manifest["consumers"][consumer]
        selected.update(policy["members"])
        for inventory in policy.get("inventories", []):
            pattern = re.compile(inventory["pattern"])
            actual = {name for name in infos if pattern.fullmatch(name)}
            expected = set(inventory["members"])
            differences.extend((name, "missing") for name in sorted(expected - actual))
            differences.extend((name, "unexpected") for name in sorted(actual - expected))
            for name in sorted(actual & expected):
                if len(infos[name]) != 1:
                    differences.append((name, "duplicate"))
    for name in sorted(selected):
        actual = infos.get(name, [])
        expected = manifest["members"][name]
        if not actual:
            differences.append((name, "missing"))
        elif len(actual) != 1:
            differences.append((name, "duplicate"))
        elif actual[0].file_size != expected["size"] or actual[0].CRC != expected["crc32"]:
            differences.append((name, "size/CRC changed"))
    if differences:
        differences = sorted(set(differences))
        visible = differences[:MAX_DIFFERENCES]
        detail = "；".join("%s [%s]" % item for item in visible)
        if len(differences) > MAX_DIFFERENCES:
            detail += "；另有 %d 项差异" % (len(differences) - MAX_DIFFERENCES)
        error = GameDataError("APK 关键资源与 %s / %s 基线不一致：%s。"
                              % (profile["version"], build, detail), "apk_resource_mismatch")
        error.members = [{"name": name, "reason": reason} for name, reason in visible]
        error.difference_count = len(differences)
        raise error
    digests = {consumer: consumer_digest(manifest, consumer) for consumer in consumers}
    identity = (next(iter(digests.values())) if len(digests) == 1 else
                hashlib.sha256(json.dumps(digests, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
    result.update(identity_mode="native-and-resource-members", resource_identity_schema=SCHEMA,
                  consumer_sha256=digests, resource_identity_sha256=identity,
                  identity_digest=identity, member_count=len(selected))
    return result


def resource_provenance(identity, consumer):
    """Produce the complete current-resource proof consumed by catalog/runtime gates."""
    digest = (identity.get("consumer_sha256") or {}).get(consumer)
    if (identity.get("legacy") or identity.get("resource_identity_schema") != SCHEMA
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        raise GameDataError("缺少已校验的资源用途证明：%s。" % consumer, "apk_resource_manifest")
    return {
        "apk_build": identity["build"], "native_sha256": identity["native_sha256"],
        "resource_identity_schema": SCHEMA, "resource_identity_scope": consumer,
        "resource_identity_sha256": digest,
        "apk_archive_directory_sha256": identity["apk_archive_directory_sha256"],
        "reference_apk_archive_directory_sha256": identity["reference_apk_archive_directory_sha256"],
    }


def validate_apk_members(apk, consumers=CONSUMERS, *, expected_build=None):
    """Validate a path/open ZipFile; return build + explicit actual/reference evidence."""
    if isinstance(consumers, str):
        consumers = (consumers,)
    consumers = tuple(sorted(set(consumers)))
    if not consumers or any(value not in CONSUMERS for value in consumers):
        raise GameDataError("未知 APK 资源用途：%r。" % (consumers,), "apk_consumer")
    try:
        if isinstance(apk, zipfile.ZipFile):
            return _validate_open(apk, consumers, expected_build)
        with zipfile.ZipFile(Path(apk)) as zf:
            return _validate_open(zf, consumers, expected_build)
    except GameDataError:
        raise
    except (zipfile.BadZipFile, RuntimeError, EOFError) as exc:
        raise GameDataError("APK 结构损坏或成员读取失败。", "apk_bad_zip") from exc
    except OSError as exc:
        raise GameDataError("读 APK 失败：%s" % exc, "apk_read_error") from exc
