from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

ROOT_FILES = {
    ".editorconfig",
    ".env.example",
    ".gitignore",
    "AGENTS.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "SKILL.md",
    "TARGET_INSTALL.md",
    "VERSION",
    "capability-manifest.json",
    "pyproject.toml",
    "requirements-audio-build.lock",
    "requirements-audio.lock",
    "requirements-portable-importer-build.lock",
    "requirements-dev.txt",
    "requirements-offline-acceptance.lock",
    "requirements-offline-audio.lock",
    "requirements-offline-bootstrap.lock",
    "requirements-offline-main.lock",
    "requirements.txt",
    "runtime-capability-contract.json",
    "setup.ps1",
}

ROOT_DIRECTORIES = {
    "assets",
    "audio_sound",
    "data",
    "docs",
    "examples",
    "presets",
    "prompts",
    "references",
    "rules",
    "schemas",
    "scripts",
    "skills",
    "tests",
    "tools",
    "workflows",
}

EXCLUDED_PREFIXES = {
    ".github",
    "docs/superpowers",
    # Large, hash-pinned companion inputs are consumed by the offline-deps
    # builder from committed bytes but must never be copied into the general
    # application archive.
    "scripts/release/ffmpeg_assets",
}

EXCLUDED_FILES = {
    "data/tmall_oral_video_style_template.json",
    "tests/test_full_release.py",
    "tests/test_full_release_acceptance.py",
}

RELEASE_TEST_FILES = {
    "tests/test_capability_manifest.py",
    "tests/test_full_distribution_docs.py",
    "tests/test_install_repo_skills.py",
    "tests/test_no_git_release_contracts.py",
    "tests/test_offline_bundle.py",
    "tests/test_private_subject_assets_release.py",
    "tests/test_repository_contracts.py",
    "tests/test_revision_runner.py",
    "tests/test_revision_markers.py",
    "tests/test_runtime_capability_audit.py",
    "tests/test_segmented_audio_delivery.py",
    "tests/fixtures/audio_sound/fixture_respiro.wav",
}

FORBIDDEN_COMPONENTS = {
    ".git",
    ".codex",
    ".venv",
    ".venv-audio",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "tmp",
    "output",
    "logs",
    "log",
    "cache",
    "cloud_cache",
}

_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    "conin$",
    "conout$",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
    "com\u00b9",
    "com\u00b2",
    "com\u00b3",
    "lpt\u00b9",
    "lpt\u00b2",
    "lpt\u00b3",
}


@dataclass(frozen=True)
class ReleaseFinding:
    code: str
    path: str
    line: int | None
    summary: str


def normalize_archive_path(raw_path: str) -> str:
    if not raw_path or "\\" in raw_path:
        raise ValueError(f"archive path is not canonical POSIX: {raw_path!r}")
    if raw_path.startswith("/") or re.match(r"^[A-Za-z]:", raw_path):
        raise ValueError(f"archive path must be relative: {raw_path!r}")
    if "//" in raw_path:
        raise ValueError(f"archive path contains an empty component: {raw_path!r}")
    raw_parts = raw_path.split("/")
    if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError(f"archive path is unsafe: {raw_path!r}")
    for part in raw_parts:
        if any(character in '<>:"|?*' for character in part) or any(
            ord(character) < 32 for character in part
        ):
            raise ValueError(f"archive path has an unsafe Windows component: {raw_path!r}")
        if part.endswith((".", " ")):
            raise ValueError(f"archive path has a noncanonical Windows component: {raw_path!r}")
        if part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES:
            raise ValueError(f"archive path uses a reserved Windows name: {raw_path!r}")
    parts = PurePosixPath(raw_path).parts
    return PurePosixPath(*parts).as_posix()


def is_forbidden_path(raw_path: str) -> bool:
    try:
        path = normalize_archive_path(raw_path.rstrip("/"))
    except ValueError:
        return True
    lowered = path.casefold()
    parts = tuple(part.casefold() for part in PurePosixPath(path).parts)
    if any(part in FORBIDDEN_COMPONENTS for part in parts):
        return True
    if any(part.startswith(".venv") for part in parts):
        return True
    if any(part.startswith(".env") and part != ".env.example" for part in parts):
        return True
    if lowered.endswith((".pyc", ".pyo", ".log")):
        return True
    filename = PurePosixPath(lowered).name
    if filename.endswith(".local.csv"):
        return True
    if filename in {
        "auto-cut-notifications.local.json",
        "project-bindings.json",
    }:
        return True
    if "subject-pointer-profiles.local" in parts:
        return True
    if "auto-cut-notification-receipts.local" in parts:
        return True
    return False


def _is_explicitly_excluded(path: str) -> bool:
    if path in EXCLUDED_FILES:
        return True
    if any(path == prefix or path.startswith(f"{prefix}/") for prefix in EXCLUDED_PREFIXES):
        return True
    name = PurePosixPath(path).name
    if len(PurePosixPath(path).parts) == 1 and (
        name.startswith("edit_") or name.startswith("make_tmall_")
    ):
        return True
    return False


def collect_release_paths(tracked_paths: Iterable[str]) -> list[str]:
    selected: list[str] = []
    for raw_path in tracked_paths:
        path = normalize_archive_path(raw_path)
        if is_forbidden_path(path) or _is_explicitly_excluded(path):
            continue
        parts = PurePosixPath(path).parts
        if len(parts) == 1:
            if path in ROOT_FILES:
                selected.append(path)
            continue
        if parts[0] == "tests" and path not in RELEASE_TEST_FILES:
            if not path.startswith("tests/audio_sound/"):
                continue
        if parts[0] in ROOT_DIRECTORIES:
            selected.append(path)
    return sorted(set(selected))


def _looks_like_nonpath_placeholder(path: str, value: str) -> bool:
    lowered = value.casefold()
    if (
        path
        in {
            "scripts/release/release_policy.py",
            "tests/audio_sound/test_bootstrap.py",
        }
        and lowered.strip("'\"") == "token=" + "secret"
    ):
        return True
    pure_path = PurePosixPath(path)
    parts = pure_path.parts
    placeholder_scope = (
        bool(parts)
        and parts[0] in {"docs", "examples", "prompts", "references", "rules", "skills", "tests"}
        or len(parts) == 1
        and path in ROOT_FILES
        or pure_path.name.casefold().endswith(".example")
    )
    if not placeholder_scope:
        return False
    stripped = value.strip()
    assignment = re.search(r"(?i)[:=]\s*(?:Bearer\s+)?(?P<value>[^\s,;#}\]]+)\s*$", stripped)
    candidate = assignment.group("value") if assignment else stripped
    candidate = candidate.strip("'\"").casefold()
    if re.fullmatch(r"oc_example_[a-z0-9_-]+", candidate):
        return True
    return candidate in {
        "token-value",
        "image-token",
        "new-console-secret",
        "legacy-secret",
        "placeholder",
        "example-user",
        "<user>",
        "<repo-root>",
        "\\path\\to\\",
        "/path/to/",
    }


_AUDITED_NONSECRET_CREDENTIAL_VALUES = {
    "scripts/release/release_policy.py": {
        "existing_token",
        "existing_api_key",
    },
    "scripts/utils/volc_asr_onboarding.py": {
        "legacy",
        "api_key",
        "x-api-app-key",
        "x-api-access-key",
        "x-api-key",
    },
}


def _is_audited_nonsecret_credential_value(path: str, value: str) -> bool:
    stripped = value.strip()
    assignment = re.search(r"(?i)[:=]\s*(?:Bearer\s+)?(?P<value>[^\s,;#}\]]+)\s*$", stripped)
    candidate = assignment.group("value") if assignment else stripped
    candidate = candidate.strip("'\"").casefold()
    return candidate in _AUDITED_NONSECRET_CREDENTIAL_VALUES.get(path, set())


_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:[A-Z]:(?:\\+|/+)[^\s'\"<>|]+|"
    r"(?<![A-Za-z0-9_.:\\/])(?:\\{2,}|//)[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(?:\\+|/+)[^\s'\"<>|]+)"
)
_POSIX_ABSOLUTE_PATH = re.compile(
    r"(?P<boundary>^|[\s'\"=:(\[{,])"
    r"(?P<value>/(?!/)(?:[^\s'\"<>|/,;)\]}]+(?:/[^\s'\"<>|/,;)\]}]+)*)?)"
)
_PATHISH_ASSIGNMENT = re.compile(r"(?i)['\"]?(?P<key>[A-Za-z_][A-Za-z0-9_-]*)['\"]?\s*[:=]\s*$")
_WEB_URL = re.compile(r"(?i)\b(?:https?|wss?)://[^\s'\"<>]+")
_AUDITED_BUILD_ROOT_ASSERTION = re.compile(r"(?i)d:(?:\\+|/+)codex")
_HTTP_ROUTE_CONTEXT = re.compile(r"(?i)\b(?:DELETE|GET|HEAD|OPTIONS|PATCH|POST|PUT)\s*$")
_SHELL_PATH_CONTEXT = re.compile(r"(?i)\b(?:cd|popd|pushd)\s*$")
_WEB_RESOURCE_KEY_TOKENS = {"endpoint", "href", "route", "src", "uri", "url"}
_PATH_KEY_TOKENS = {
    "asset",
    "dir",
    "directory",
    "draft",
    "file",
    "folder",
    "path",
    "root",
    "secret",
    "source",
}
_POSIX_SINGLE_COMPONENT_ROOTS = {
    "/bin",
    "/boot",
    "/data",
    "/dev",
    "/etc",
    "/home",
    "/lib",
    "/lib64",
    "/media",
    "/mnt",
    "/opt",
    "/private",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/srv",
    "/sys",
    "/tmp",
    "/usr",
    "/var",
    "/workspace",
}
_PATH_FINDING_CODES = {
    "absolute_project_binding",
    "build_repository_path",
    "user_profile_path",
}

_AUDITED_BINARY_ASSET_DIGESTS = frozenset(
    """
0487d9edc7d12ba13e0f8e74d84f1a2550b3b5f10927e9e218286e43b2d63ec2
07a71dd511fa953a1d11d7d698a998554d458b75162269a738ef0ae0f41be9a6
0af047f82238408624ecdee6db1f1e3d8bc014557fc951c0b3cab255dff06cd1
1fa74f935936fb4f5bb3ca5a30297df832088bf619abbd572601b747c2b24b69
2f7b3aa4745e89ddc99e58f3b3312d060873bb6429c08513d32c06ea437239c5
2e4e8aae3e782c64c9c53bc7a41b8271cd241ab865972c5377461cb43fb37191
2f878f12171637f3428d198b6c1eaf8037d5ab8db55a3665c149f0b6dcb060fa
2ffa9404ca92678a433d67e2f3e0cd32d4ed959f30678bd43ade5dda05a628f7
32f9cdbe4ca6bf17dbdca8f106530fbc5827934866d2184a81c039a11fc3c7e6
3f131e3b34778efafc14877700692f87d37295add48559eb37c4ff5ee8091848
4358064dc7a778c73e7105b54b9c63355c6a2b605104a0ccf58c4ff1ff6f0cf7
49d6123631f225b1b85bc3ebcd381204559b581b5992ccdae96ecce7aadc5d12
6f3b45b9959241d544c2c6fb0ca9c97408e3ee2e19c33921c4c29802eb46f622
618e5c1ec06e7dc395f4654320259a3a30d457f1e0c5bf3b05a5c73e0307894a
6df26ae12df64596aafea76522387369de9fd8d0c92a65e1246d3bb4b7d791de
746560e1157ca4c77cb84d4afd120015a17727b63c60d4137107eaaa4d070e34
77acd48bdafd0b070b8010c243b58f3cce38fca29c1b0d28761ba041818be267
77dfcb6ed774b81696811451c32873c1eb53f73db47823891dfe23459642fc9f
7f798d1a087905eae1c8e329b5cf5e987f4adbacf6f8982b73880680ae9651c4
8a79f1c0207a85cfd2c57f608f597743f43578758286025a3be34f848339fa14
917fe6f663c03c432a13045e2366bf431175d92b5cc94a048c46bf8e471a5541
99675825a64b03900b4d12ac404ddd189729721498ba12c956288da56bfa3dba
a85becb0858ccc0342230c686413dc112b6e1aa9380ba0b1fc7da1a54ef346f7
a7c39195abcf461e60af5a1f48180bc5df510cc50a4d7fe99193128c9a55fe11
bbfe494206138c8a9648fd96b99b1c7a53e3520343625f8041479ee18d1585b2
bc579a8563aa8eb75a2c0ada7ab109168432580b71b30235a7dd713292a3ed18
cbd041fd662c6db193d9dde29e2fe369bbea9fdd6275493c6e7208e2141e93b0
c6f04f57d1d3c4e5fe21c817d77ee023457bf31f5bb9bb079022a54f213928a8
d4eacffae047df34b0ea6d8bd1627a4f5ae97c38686b50d19bbff33e075aabd0
d4af526a226b5c930020d7ec3a2f699dc329bb2fb42698df86851b318b7c58b7
e27fbb3dca26f18a0f40686e5b983a28a047eace9189f06747b69a8dde1321f6
ea1999c9b89e150a21c0167cd018bf0ec7016f59881d3a2e62c6e6988b85c7cb
f3a8934417248329c82fdbea033396550d4a2573fdb50dc3dbe9816ccb79d36d
fb04bc7868001dfba9a334a6f8a54658b981498e222c7730c16459afac4b7850
f55ac28f4ef9595a899717de5cf36752e769f6c689e973698b601d9f4c15e735
""".split()
)

_AUDITED_ABSOLUTE_PATH_EXAMPLE_DIGESTS = frozenset(
    """
0536bfa57a43955bf9cf6152f8decd8120638d10162c502a5e3e8b2b33ae1844
0755ed01d19cd6e6ac25aefa7acf283840e54f375f05f139d948656feabbf8f2
09d74dd114c701f9da887fe57f49783e04e0bf5a18be922927e2e16119011195
0c377c88f058889ec06f1049ba95c7980bcdd30f53337ba3a26cbfc289b7fc55
0ed865f27600082090245a6ee52bbb01e937cff4a021a3d4efbbca21b5b896c8
0fffa61aa5a6a696e0f566f8a3a697f61dbf2b87ce1998b730b36e7460a2108c
12617b5a2f6c4098d9439b2a626f5fcdab166a5ca36e28f73e3ca3e4d83504af
126608a147f85e770a13f8e7af0fe30f1c0acb2cfe69dd2d6643017003cbee1d
14a4cd395ec4cc5caffc4d88a2273104984dd78996fe7d6459451f7142659b06
16cab755d00d5dbd4a6a6bb6117bc7e84b515515a73050975a0dcc3559755828
172304e4cecca1c9ced1cfd1e0337e0f3f9fca6a78b77479c7ae356c8279ca05
1733ef1e8646c47960fb97badaa115bf7281420f141a1cba3dfa0e3d105bf0d7
18eaeafbafcde0c439bfb7f8ae34894019b7c326aead6f61711472bf85437e8c
199a32f8fd87770022486f2abab9328ee16fa731b53ac36ca26890e7122a7c48
1c9a31cbdd72428ca8ac226f306d03bea33eee963ab363bca1ed2b8000ae7606
1ccfc9c4fad7b0190411fc1b36f3b041d1dd4ddfbf06507aaf1a9906d9b605b6
1cf3a008ab3849143a8beb421218d923d0ab9c4508fd0ca8007a385723614ba6
1fc15c581b922b64aaca342e30ffb65388eb43135abf6240f433f64e3a9ce53e
208d19b9f8f867e25a3490dc009866cfcc633417ff8e43977365c2b4a470bc4e
25c24b2dbaf8807219c278e4bdb8d4edab9f7fe863932c797fc1318692208729
26e708981d746e53cfb7c6a3c8827446d9107ecd4518f58ab4064147e09ef9fb
274b4138c70451dcb07095d9bd64904174df2fe2b8398f70c6ddfdc3b2374d8d
27f2c1b0304ecd170d56354910bce2f5475ca93db1da82c3992d90bf751867f3
28c4db0d76b75b0f34d4456d739ea98fa13ba7609ce538281515706559999d29
2dfad1ce2a9bf59956bdd54cdfbac7efdee8b206b165d40de2e880a74321c88f
312c09283066b5a3713520fe0dcd9e7c85bdaf823401f0d86fb3761a06f4b477
317cd16d867c2138e186b62fdced2f4bfbd012ca9758c97f186886ffb90ff6ba
345dbedc4fab1724b7e44872cbd4ddd678c91b6aeea1817ab03d4c2e0d742e44
376097a65791cf0a97c21ffec1234f95b03093cfd7b6d9e1bbbe1f92b5732324
34f167f247d8d7068bd7e83328cd1e882730a2648e7a89dd2d6cad1ec73c2b1b
3541dbdb2f7e0404b09ad2d0046cc2c5a9d9e96dcebac049be9b111c06a981d2
394246b4e440a9110829c93c164991eec8c72e264884d2c1864c8ea81572d507
396e57965e6c4f6e7f575c04f4045f925117910b921dbd37c36c77014ccb6e50
3a4bf6d57412ad15197ad97dbf81f7a9fee4e342f67287c17ac6e664b500e915
3afad7ba7c3e7350a80fbb0a1b0ac37be23ddcdf667014b3a26e26f07e2174be
3c364c4866b4cb76bc4d08031231f0bf54ee48368122336c1600158edc94fc53
3ecc8127974b7f9754ee0a842a3037f0f1307660916921e290984bbecc4bf1d6
404483c8a613ac06e129a87181b46918d9540352e18ac583f2107f18a83e0f38
41d50bc7602e510caea7ae89317c7dbb8544b74f637ee0f0ef1b33699920e5c2
45b44dc1e0e36259e4b291354dd03ebd3fcdb34e1dcbf4619e138ebdefc8d050
47e616b105d2a2f0d5964a502ab6fa75aaa752e556a9f92840184e9242e52716
48044d24107f5a9335df1030367954d9e48cb90f4c8f6d89f78bca8878a5a1e9
490eebed0417ada61ce5b8856716170949060a9f15692af9d4c29cd7f3bfe361
4a48b2b791277ca80a60fd9d338969cd67637eabcead54095e9405b41b24fde3
4be9cc41483a8814729450a75f3fc37fd103d841238d631167a3d8af801dace1
4caa52cc3e6bdbe25d84c330e0291b70a011368cd3450faa8cd6d8e017135014
4d77844321563af69dd7c924a184a0a7b05cbd386fa81d1b0e2b0c72534a583c
4e2e0056759b4cdc90ef9239813fe0101f0bf0f72e5c2afd039e426c9c4051fe
4f666572e99f9ac7722bac7b7455fe71db719461dd467b44c4a3c689f6db0242
50e753b77bb3270cc3f4573b62232ca889db7e02e5ad2fc5fe14c2039f0616fb
5161363f363fa9cc69aebe9b8c5f18d06d25481203aacf79e9fde76ae0be5a6d
52b7ecc63513e39a0f46feb123353e5a65daa672c610ff5217223aa2407d0a75
533e51db7fea3ca7b2d3da74dc458660607a4ede9c39043242d23331e4ec0212
534f6ce9d1658b26cc5f7dce9b8968a4d1d40b56f7c56314e43e23cbef9389fc
5499d8322faad3a3e2cf7c2a4f52aa1b0d40f9dbcf37d56884fcfa6c7744d52d
57265599154029611209661bf35fe208a92a59fc15696efd132bfc2c0f9df200
585b8eabf5b96b2029d660b63e01ce91eafb5492f75756077940ddaed31e1732
592737f958432b0e225dc817bcdbb958eba216ed8f0963e2855929edff161ad2
597aec4134d2a0ad4f0e811a629e3b7862fe26216a32718e198253c36f42bd4d
5987c0233d9c4fe64caadd96128d4d5ad9426535d9ba8d648e56ba32e6f06714
5c5c49c2709e81b0e1f427e00a15f03be194a13afa652caba9ba8331cc043c37
5c8f632f0c2e45d821c3136fd7e8f22835f462c62532036fb56e68343bf76fd4
5d70bfbc736f613b337436f89fb692bb3b314cb7cae1facdf6ab44b8c2de166f
5dcb5aaca4c7940fb5f9ecf6dc184a286c6ceb1864544a5fd28eb3bd2cb6ea7c
5fd5609bb28f63d171dd5aadbc3b61b2bb3032d68d122acbe8025303b04a574c
61bafc96bc30520e875cddd105444bb80e3afde8ed44c56117e8e1ed173e5903
62b35b11f760a748064cb81f91e85de47e898540b4af4cc50cc87f497b83cac7
641a132ed996160f00094253ea9345f6bea8893700b99fe348d1ee69579f6555
6640fc11eaccc1c249c6c2a723501e7174619efbbe87a0e971fbdea4e0d6881f
66854f827043a9dfbcc69c6d1635934010602eaf61401b890003a5e084f10fb2
67839b4d8dd8a57a261dd861f8c619c2e05ee550619b3eabcc4e9fec3e03ecbd
6bd66e550abd1d9fc2c3a869c1a75ba2a10983dd2d0fbb8aa4e0f8c647ca7f6d
6fa4902c3d4a07df8ec259df1214361ad7f966204979b58bd38b974afeac2ede
777511ed0a1f9ac5fb11f714ddc938114d88484b7368bbbfe82ce24b0819a841
79deda9a1d2877792ffa57a11a8cdd43c9e06242d4fd5284d1bc6dc2d828ed06
7d68282856a934e37209197e4bd3ed88e0a52de803e2031a49bbf00a67c4195f
7db2a41df48a4c68f7a8e2f21a425636ea5a5867a03bbab7e4ddbc25a4cf2146
7f9a3ce6ebb57ac61f358c2c9d0be71ecbffacc626e2518069cbe1f79ec1d71a
7fda7b427c5f58c6a1706a014814ed43dc766aea16f8e5d6492ff059e834429e
81e0063a9ccb959322595cdbecb8643f3face9b1c39704a20191207fd79eb95e
82e45a097ef231b8ef032b1c9aba784a6dff7fcec3b74647017debd9f2df098b
8357a3a04f70d6cf4d21ebd739be63701919cffc28447bd7b983f79eba942b62
84075098c96bc6aef6e0cf57160e4be9e1fdfd7838fb4434b835b43480e8b1dc
84b8777a74af0399ecc50c297a7004a474efe3762f6221713ad286da5a1824ad
852e2c7ff1ebdc875816aa065982c21aa781891489f7c91a4467b8e586d75ca3
86504dbdcb24603fd39df2cfc24bae6b950ec8dc8557fe6bca34ea740897f16d
8b8987a19e74a5c31079aa9a3b4509c9880afb50a0229e28f070903258c53a37
8c87ca3a255e47be2768df98a96afcc920a2b15918f95bb6401a1dad1c7d69ac
8e4696c11f66da5f3aed083cdad2ea3728baaab0fe4ee0dd526fa2094e13f1dd
8f0d6a7b9fb8a03ff955d3a1e53bedc6f7e9345ff677fe0a44634d84190c53c9
90eaec4ddf2f77cd28beda8cf0358f7c3039c2b59cb51f01af547744971447eb
911134a662a8479328e34fc9c1bd3feb65b8bb03a2687f2fa02319aa426263e8
917e531b0a7a8039be6ea7af0464b28a72517259ad9d5f5b9c721789ce923f1c
92176196335afe1d7b03f7d6927ee481b76c67bcbcafdcd400ffae84254bec6c
929d1f5ba1fd3105c5f3991d7a701a72e5fa7eccb9971836a377e29d39a7607c
92ffeb50da59564fd50ec66335d6df03ecc80fa455595084be5518f41191d9d2
9362c691990d1de074804758a9c6052d9951d44d4519d2aa8ac103c776cf2446
93f58c1c669ec7431ba15507db22b2ae9c26377ac33bfabff04720ee379530b1
94fff3b1c4be3b3a552e84f546a4c323ac897a8bd8b176ca02a21f66281224f3
9931887f806218805eda7c4361c992ce975ebc3a307cf5319b1e560971db6618
9a47e112d415b5b545a1652c58d2e7d53ad496d786ee6bdccec79d67822304f2
9f42e8fe0881c5b92e34ef369437053262c2297406e8a82b8f81b48655c866ff
9f7b3330c32ca00b9dc21ce8bd574e245668de90059ad54ab81efed4c012f92c
a31e32a2d814e34b2bb603116686696aab94ea7937086c49783f04aa2fe3e950
a68e41663d57bdabc3d9355703f1eac0bd97a07fffd3bf0c033df933e5ef111f
a6ff86915790012ca259b732799289592445b49a75cfc76f3157f3bf8e70f3aa
a81ef43c898b3da8eae76b881085bbda197e4ccf2d33682fd32227d13b24c1c5
a9024fff7c584f12ff796f72d8e8dc7d7a50998a3c0531e7b94ff0fdebfefc0e
a95127bd3561eaec969d064e935de3fe35812b1ebfeda85e702a252501ad40b0
ace005ab411668bd60c16cc0242ee02f686c66237167666b56f3b59903d65c1f
afe38687011c638b20ae4e21b2189347c61ad4fd13aed3936e1dad73ef56ca58
b150d6c611a62499f3ed723bb7a21e2469d2069f6cee9945c9093b3a37deb56b
b20aa7d77bb8e183cc1f7a648715bbec4a683bc35234f3896279d3dad6710932
b2c416faf0d8c1d3ff14b98cc4fd5961aebbf28737dbe7d5ce0506af61f7ae4c
b7cad4ed715348e5d1387a9ad5d414fe08775b0a6cf62b9ff6d2e2503e057fed
b8061afec82ad359e7965084ab82ef8a87368856272215e90bd5d9c31867d784
b9a2b84f4e0916f1d27b2ef412fa73a596fa0bc4fa1a30598b82be8023841e33
c0663346486da1a70d6f320a3a40dd6bdef860c5dd01d0618f0978648f0b49d7
c09c5a4e5366d3ff4ac21c3aa8beed05da09526afea17baaca24822a68802b9b
c1d8420d07a9af333576ad87a90c09ab4e66decb704a99425ba5d3ee3946a66d
c47b7c15b0b325f65054a12d8cc1a6d0464d4eb4ca54bf396ecfa43bf21ca384
c624f3d4cfb558e96a981d249ff87fb698e283cd0d92d23a0e45c67bbfaddd6b
cc83df88d7be574542a5fc36925a1d1bbe1cf73a8b86ce1281302a853a2beb9b
cdc5fed792eb89a21b91667fc889f0ce809f71e9be5555455684c78742443f1e
cec7d036bdc57c80b57d0ae9f97e954b94409aa837c580babd098ef6786d20bd
cf6b13322b4a34d0f0376bc2a7a89548b0bb2f088efddf97eee8f6125d6b5e66
d1368fe2bb5d0bd678de85d19213ffbd39f13b634a5bc7146e40d438391096ff
d8d22580521728688d8f3bf89608b048255b97a3c88f49a1ce4c72aa75ca831c
d7e3125369c8c79377b20c0963b85d8628cbd8e8b8735016bab23d182b184fb3
e11c48708b0d85b5710222d990b1bd7906a97104b623413e7175cf70e7bf004d
e142f716dd362ba8726a902db007be175e14c39cf8908b30daad36f98e7ceee0
e858ec716c025d50740dde29a3ea52aad1ac325f4d81328293e2c6b49bf8bfbd
ec2d23fb729801a96f25f6560488c8ad4dfada5dced676cd478784e0c35d23d9
ef3f63039cc7ae5c48b708cdcdf45d2c94609d6dbc92b8d71a197b4422430755
ef6b88050eed0e4564ab5f18f0b13214dc31c2d098b4d735f99850e60bd97e50
efcfa911b5f782cef289cc528612a7bcbec28910edb46ac54f282a947dd7998b
f16594010601037f423f7d092bd9a56b7e494db459842c956cb725eb4081e7cb
f309cf47df07dc3f05304b844846a6ed9d9b56bf7b56b97413a982ae3c865b7b
f33ad06161774eff9b8a20a60c15a6477c6d30ab11a6e9403185cbef7bb3d49a
f52a885eebd1373ee0bcc57bc9aa60eabc1f0d0f2842a561678dbdd240371753
f7f7ab6139a3e3dcc6444459507d3b9ba79d54c4503a387f4b922bc39f01b9f7
f92f11053ecfd6bda4278388e71769258daba610b4c41ef10b929f111a2221fa
ff4a8d376d156d57f028d880639133f72df24a92d8eca7c9d2432b787f55726d
117f9d3a217aef579a2a09309354e460736fd4d537052aa97a1e706241966fb8
14cabd2767e47a835b59094795e39452e80dde26181303664b4286014e0332bc
2f4ed543ce2a757c481c4da71c796d471825e1e8678483c0af28633e12ab1c60
512e214c67109f348a38aa96287367bba7fc036685a5ad8ee7e84345ad8d490f
543de4d0c9829e53d713c989701018dc8846e662fd29480bf6857a74dcd6b915
8e2936e103e8c5f7b0e64da68f1c8ff12788dda5a320b5247e8f5ba8a9fd4588
d7ebecf7a77334c0763aaaaa5667faaa96eedf7888850d5e6dcb230fa5f36e39
f5db382557580901db9fcafc8fa4549e182bbe00045a20cb22fb8d31a88bcb63
1ba5b99e446396cd375d0c94ece76f7228fcfcb242fcaaf34a495be55afe8894
1e430755bf4035c12da549420580a2185a0484ac7cad0b6626b977a7e8ea289e
c46132e8a61e4e0f62a49d367cdaf21a454035c390fa1979d696c43e0a96aa3b
""".split()
)

_AUDITED_PORTABLE_PATH_LITERALS = {
    "scripts/jy_http_server.py": ('"/tools/"',),
    "scripts/core/mocking_ops.py": ("c:/program",),
    "scripts/utils/jianying_env.py": ("c:\\program",),
    "tests/audio_sound/test_bootstrap.py": ("c:/tools/",),
    "scripts/release/release_policy.py": (
        '"/tools/"',
        "/tools",
        "/tools/",
        "c:/tools/",
        "c:/program",
        "c:\\program",
        "c:/media/",
        "c:\\media\\",
        "d:/media/",
        "d:\\media\\",
        "\\path\\to\\",
        "/.",
        "/path/to",
        "/path/to/",
        '"/path/to/"',
        *_POSIX_SINGLE_COMPONENT_ROOTS,
    ),
}


def _is_audited_absolute_path_example(path: str, value: str) -> bool:
    normalized = re.sub(r"\\+", r"\\", value.casefold())
    digest = hashlib.sha256(path.encode("utf-8") + b"\0" + normalized.encode("utf-8")).hexdigest()
    if digest in _AUDITED_ABSOLUTE_PATH_EXAMPLE_DIGESTS:
        return True
    return normalized in _AUDITED_PORTABLE_PATH_LITERALS.get(path, ())


def _assignment_key_tokens(line: str, match_start: int) -> set[str]:
    prefix = line[:match_start].rstrip("'\"")
    assignment = _PATHISH_ASSIGNMENT.search(prefix)
    key = assignment.group("key") if assignment else ""
    return {token for token in re.split(r"[_-]+", key.casefold()) if token}


def _argparse_parser_names(tree: ast.AST) -> set[str]:
    parser_names: set[str] = set()
    subparser_action_names: set[str] = set()
    assignments = [node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))]
    changed = True
    while changed:
        changed = False
        for assignment in assignments:
            value = assignment.value
            if not isinstance(value, ast.Call):
                continue
            targets = (
                assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            )
            names = {name.casefold() for target in targets for name in _assignment_names(target)}
            function = value.func
            is_argument_parser = (
                (
                    isinstance(function, ast.Attribute)
                    and isinstance(function.value, ast.Name)
                    and function.value.id == "argparse"
                    and function.attr == "ArgumentParser"
                )
                or isinstance(function, ast.Name)
                and function.id == "ArgumentParser"
            )
            receiver = (
                _container_root_name(function.value)
                if isinstance(function, ast.Attribute)
                else None
            )
            if is_argument_parser or (
                isinstance(function, ast.Attribute)
                and function.attr == "add_parser"
                and receiver in subparser_action_names
            ):
                before = len(parser_names)
                parser_names.update(names)
                changed = changed or len(parser_names) != before
            elif (
                isinstance(function, ast.Attribute)
                and function.attr == "add_subparsers"
                and receiver in parser_names
            ):
                before = len(subparser_action_names)
                subparser_action_names.update(names)
                changed = changed or len(subparser_action_names) != before
    return parser_names


def _is_argparse_option_literal(value: str) -> bool:
    return (
        len(value) > 1
        and value.startswith("-")
        and value not in {"--"}
        and not any(character.isspace() or character in "/\\" for character in value)
    )


def _is_argparse_option_argument(
    call: ast.Call, argument_index: int, parser_names: set[str]
) -> bool:
    if not (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "add_argument"
        and _container_root_name(call.func.value) in parser_names
    ):
        return False
    value = _static_string_value(call.args[argument_index])
    return value is not None and _is_argparse_option_literal(value)


def _python_cli_literal_spans(text: str) -> dict[int, list[tuple[int, int]]]:
    try:
        tree = ast.parse(text)
    except (RecursionError, SyntaxError, ValueError):
        return {}
    spans: dict[int, list[tuple[int, int]]] = {}
    parser_names = _argparse_parser_names(tree)
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ):
            continue
        for argument_index, argument in enumerate(node.args):
            if not (
                isinstance(argument, ast.Constant)
                and isinstance(argument.value, str)
                and _is_argparse_option_argument(node, argument_index, parser_names)
                and hasattr(argument, "end_col_offset")
            ):
                continue
            spans.setdefault(argument.lineno, []).append(
                (argument.col_offset, argument.end_col_offset)
            )
    return spans


def _python_add_argument_path_lines(text: str) -> set[int]:
    try:
        tree = ast.parse(text)
    except (RecursionError, SyntaxError, ValueError):
        return set()
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ):
            continue
        for argument in node.args:
            value = _static_string_value(argument)
            if value is not None and value.startswith("/"):
                lines.add(argument.lineno)
    return lines


_CREDENTIAL_NAME = re.compile(
    r"(?ix)"
    r"(?:[A-Za-z][A-Za-z0-9]*[_-])*"
    r"(?:access[_-]?token|refresh[_-]?token|api[_-]?key|app[_-]?secret|"
    r"client[_-]?secret|authorization[_-]?url|credential|device[_-]?code|"
    r"password|token)"
)
_GENERIC_CREDENTIAL_LITERAL_ASSIGNMENT = re.compile(
    r"(?ix)(?<![A-Za-z0-9_])['\"]?"
    r"(?:[A-Za-z][A-Za-z0-9]*[_-])*"
    r"(?:access[_-]?token|refresh[_-]?token|api[_-]?key|app[_-]?secret|"
    r"client[_-]?secret|authorization[_-]?url|credential|device[_-]?code|"
    r"password|token)\b['\"]?\s*[:=]\s*(?:Bearer\s+)?"
    r"(?:\"[^\"\r\n]{6,}\"|'[^'\r\n]{6,}'|"
    r"(?=[A-Za-z0-9._~+/=?&:%-]{6,}(?=$|[]\s,;#'\"})]))"
    r"(?=[A-Za-z0-9._~+/=?&:%-]*[-0-9+/=?&:%])"
    r"[A-Za-z0-9][A-Za-z0-9._~+/=?&:%-]{5,})"
    r"(?=$|[]\s,;#})])"
)


def _is_credential_name(value: str) -> bool:
    return _CREDENTIAL_NAME.fullmatch(value) is not None


def _credential_reference_name_matches(value: str, allowed_references: set[str]) -> bool:
    return value.casefold() in allowed_references


def _container_root_name(node: ast.AST) -> str | None:
    current = node
    while isinstance(current, (ast.Attribute, ast.Subscript)):
        current = current.value
    return current.id.casefold() if isinstance(current, ast.Name) else None


def _is_runtime_value_reference(
    node: ast.AST,
    allowed_references: set[str],
    trusted_containers: set[str],
) -> bool:
    if isinstance(node, ast.Name):
        return _credential_reference_name_matches(node.id, allowed_references)
    if isinstance(node, ast.Attribute):
        root_name = _container_root_name(node.value)
        return root_name in trusted_containers and _credential_reference_name_matches(
            node.attr, allowed_references
        )
    if isinstance(node, ast.Subscript):
        key = _static_string_value(node.slice)
        key_name = (
            key if key is not None else node.slice.id if isinstance(node.slice, ast.Name) else ""
        )
        root_name = _container_root_name(node.value)
        return root_name in trusted_containers and _credential_reference_name_matches(
            key_name, allowed_references
        )
    if isinstance(node, ast.BoolOp):
        return all(
            _is_runtime_value_reference(value, allowed_references, trusted_containers)
            for value in node.values
        )
    if isinstance(node, ast.IfExp):
        return _is_runtime_value_reference(
            node.body, allowed_references, trusted_containers
        ) and _is_runtime_value_reference(node.orelse, allowed_references, trusted_containers)
    return isinstance(node, ast.Constant) and node.value is None


def _contains_credential_container_reference(node: ast.AST, allowed_references: set[str]) -> bool:
    if isinstance(node, ast.Attribute):
        return _credential_reference_name_matches(node.attr, allowed_references)
    if isinstance(node, ast.Subscript):
        key = _static_string_value(node.slice)
        key_name = (
            key if key is not None else node.slice.id if isinstance(node.slice, ast.Name) else ""
        )
        return _credential_reference_name_matches(key_name, allowed_references)
    if isinstance(node, ast.BoolOp):
        return any(
            _contains_credential_container_reference(value, allowed_references)
            for value in node.values
        )
    if isinstance(node, ast.IfExp):
        return _contains_credential_container_reference(
            node.body, allowed_references
        ) or _contains_credential_container_reference(node.orelse, allowed_references)
    return False


def _single_line_span(node: ast.AST) -> tuple[int, int, int] | None:
    line = getattr(node, "lineno", None)
    end_line = getattr(node, "end_lineno", None)
    start = getattr(node, "col_offset", None)
    end = getattr(node, "end_col_offset", None)
    if line is None or line != end_line or start is None or end is None:
        return None
    return line, start, end


def _python_credential_passthrough_spans(
    relative_path: str, text: str
) -> tuple[dict[int, list[tuple[int, int]]], set[int]]:
    try:
        tree = ast.parse(text)
    except (RecursionError, SyntaxError, ValueError):
        return {}, set()

    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

    def scope_for(node: ast.AST) -> ast.AST:
        parent = parents.get(node)
        while parent is not None:
            if isinstance(
                parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
            ):
                return parent
            parent = parents.get(parent)
        return tree

    spans: dict[int, list[tuple[int, int]]] = {}
    rejected_lines: set[int] = set()
    bindings: dict[tuple[ast.AST, str], list[ast.AST]] = {}
    mutated_containers: dict[ast.AST, set[str]] = {}
    for assignment in ast.walk(tree):
        if not isinstance(assignment, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            continue
        if isinstance(assignment, ast.AnnAssign) and assignment.value is None:
            continue
        targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
        scope = scope_for(assignment)
        for target in targets:
            for name in _assignment_names(target):
                bindings.setdefault((scope, name.casefold()), []).append(assignment.value)
            credential_name: str | None = None
            if isinstance(target, ast.Attribute):
                credential_name = target.attr
            elif isinstance(target, ast.Subscript):
                credential_name = _static_string_value(target.slice)
            if credential_name is not None and _is_credential_name(credential_name):
                root_name = _container_root_name(target.value)
                if root_name is not None:
                    mutated_containers.setdefault(scope, set()).add(root_name)

    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        if not (
            isinstance(call.func, ast.Name)
            and call.func.id == "setattr"
            and len(call.args) >= 2
            and _is_credential_name(_static_string_value(call.args[1]) or "")
        ):
            continue
        root_name = _container_root_name(call.args[0])
        if root_name is not None:
            mutated_containers.setdefault(scope_for(call), set()).add(root_name)

    def approved_binding(value: ast.AST) -> bool:
        if isinstance(value, ast.Constant) and value.value in {None, ""}:
            return True
        if not (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)):
            return False
        if relative_path == "audio_sound/volc_asr.py" and value.func.id == "_config_value":
            credential_names = {
                "VOLC_ASR_ACCESS_TOKEN",
                "VOLC_SPEECH_ACCESS_TOKEN",
                "VOLC_ASR_API_KEY",
                "VOLC_SPEECH_API_KEY",
            }
            return (
                not value.keywords
                and len(value.args) >= 2
                and isinstance(value.args[0], ast.Name)
                and value.args[0].id == "env_values"
                and all(
                    _static_string_value(argument) in credential_names
                    for argument in value.args[1:]
                )
            )
        if relative_path != "scripts/utils/volc_asr_onboarding.py":
            return False
        if value.func.id == "_resolved_value":
            return (
                not value.keywords
                and len(value.args) == 3
                and isinstance(value.args[0], ast.Name)
                and value.args[0].id in {"file_values", "values"}
                and isinstance(value.args[1], ast.Name)
                and value.args[1].id == "environment"
                and isinstance(value.args[2], ast.Name)
                and value.args[2].id in {"ACCESS_TOKEN_KEY", "API_KEY_KEY"}
            )
        if value.func.id == "_validate_single_line" and len(value.args) == 2:
            prompt_call, field_name = value.args
            return (
                not value.keywords
                and isinstance(prompt_call, ast.Call)
                and isinstance(prompt_call.func, ast.Name)
                and prompt_call.func.id == "secret_input_fn"
                and not prompt_call.keywords
                and len(prompt_call.args) == 1
                and bool(_static_string_value(prompt_call.args[0]))
                and isinstance(field_name, ast.Name)
                and field_name.id in {"ACCESS_TOKEN_KEY", "API_KEY_KEY"}
            )
        return False

    def allowed_references(
        scope: ast.AST, expected_name: str, current_value: ast.AST | None
    ) -> set[str]:
        expected = expected_name.casefold()
        expected_bindings = [
            value for value in bindings.get((scope, expected), ()) if value is not current_value
        ]
        allowed = (
            {expected}
            if not expected_bindings or all(approved_binding(value) for value in expected_bindings)
            else set()
        )
        fallback_name = {
            "access_token": "existing_token",
            "api_key": "existing_api_key",
        }.get(expected)
        if fallback_name is not None:
            fallback_bindings = bindings.get((scope, fallback_name), ())
            if not fallback_bindings or all(approved_binding(value) for value in fallback_bindings):
                allowed.add(fallback_name)
        return allowed

    def required_parameter_names(scope: ast.AST) -> set[str]:
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return set()
        arguments = scope.args
        positional = [*arguments.posonlyargs, *arguments.args]
        required_positional = positional[: len(positional) - len(arguments.defaults)]
        required_keyword_only = [
            argument
            for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults)
            if default is None
        ]
        names = {
            argument.arg.casefold() for argument in [*required_positional, *required_keyword_only]
        }
        if arguments.vararg is not None:
            names.add(arguments.vararg.arg.casefold())
        if arguments.kwarg is not None:
            names.add(arguments.kwarg.arg.casefold())
        return names

    def trusted_containers(scope: ast.AST) -> set[str]:
        trusted = {
            name for name in required_parameter_names(scope) if not bindings.get((scope, name))
        }
        trusted.update(
            name
            for (binding_scope, name), values in bindings.items()
            if binding_scope is scope
            and values
            and all(
                not (isinstance(value, ast.Constant) and value.value in {None, ""})
                and approved_binding(value)
                for value in values
            )
        )
        return trusted - mutated_containers.get(scope, set())

    def reference_is_safe(node: ast.AST, scope: ast.AST, allowed: set[str]) -> bool:
        return _is_runtime_value_reference(node, allowed, trusted_containers(scope))

    def reject_untrusted_container_reference(node: ast.AST, allowed: set[str], line: int) -> None:
        if _contains_credential_container_reference(node, allowed):
            rejected_lines.add(line)

    def add_span(node: ast.AST) -> None:
        span = _single_line_span(node)
        if span is not None:
            line, start, end = span
            spans.setdefault(line, []).append((start, end))

    for node in ast.walk(tree):
        scope = scope_for(node)
        if isinstance(node, ast.keyword) and node.arg is not None and _is_credential_name(node.arg):
            allowed = allowed_references(scope, node.arg, None)
            if reference_is_safe(node.value, scope, allowed):
                add_span(node)
            else:
                reject_untrusted_container_reference(node.value, allowed, node.value.lineno)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            value = node.value
            if value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            credential_names = [
                name
                for target in targets
                for name in _assignment_names(target)
                if _is_credential_name(name)
            ]
            safe = False
            for name in credential_names:
                allowed = allowed_references(scope, name, value)
                if reference_is_safe(value, scope, allowed):
                    safe = True
                else:
                    reject_untrusted_container_reference(value, allowed, value.lineno)
            if safe:
                add_span(node)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (
                    key is not None
                    and (credential_name := _static_string_value(key)) is not None
                    and _is_credential_name(credential_name)
                ):
                    allowed = allowed_references(scope, credential_name, None)
                    if reference_is_safe(value, scope, allowed):
                        key_span = _single_line_span(key)
                        value_span = _single_line_span(value)
                        if (
                            key_span is not None
                            and value_span is not None
                            and key_span[0] == value_span[0]
                        ):
                            spans.setdefault(key_span[0], []).append((key_span[1], value_span[2]))
                    else:
                        reject_untrusted_container_reference(value, allowed, value.lineno)
    return spans, rejected_lines


def _python_negative_assertion_literal_spans(text: str) -> dict[int, list[tuple[int, int]]]:
    try:
        tree = ast.parse(text)
    except (RecursionError, SyntaxError, ValueError):
        return {}

    spans: dict[int, list[tuple[int, int]]] = {}
    for assertion in (node for node in ast.walk(tree) if isinstance(node, ast.Assert)):
        comparison = assertion.test
        if not isinstance(comparison, ast.Compare):
            continue
        operands = [comparison.left, *comparison.comparators]
        for index, operator in enumerate(comparison.ops):
            if not isinstance(operator, ast.NotIn):
                continue
            literal = operands[index]
            literal_value = _static_string_value(literal)
            if literal_value is None or not _AUDITED_BUILD_ROOT_ASSERTION.fullmatch(literal_value):
                continue
            span = _single_line_span(literal)
            if span is not None:
                line, start, end = span
                spans.setdefault(line, []).append((start, end))
    return spans


def _span_is_ignored(
    start: int, ignored_spans: Sequence[tuple[int, int]], source_line: str
) -> bool:
    byte_start = len(source_line[:start].encode("utf-8"))
    return any(span_start <= byte_start < span_end for span_start, span_end in ignored_spans)


def _posix_candidate_is_local_path(line: str, match_start: int, value: str) -> bool:
    if "/" in value[1:]:
        return True
    if value.startswith("/.") or value.casefold() in _POSIX_SINGLE_COMPONENT_ROOTS:
        return True
    if _assignment_key_tokens(line, match_start) & _PATH_KEY_TOKENS:
        return True
    prefix = line[:match_start].rstrip("'\"")
    return bool(_SHELL_PATH_CONTEXT.search(prefix))


def _absolute_local_path_matches(
    line: str, *, ignored_spans: Sequence[tuple[int, int]] = ()
) -> list[str]:
    url_spans = [match.span() for match in _WEB_URL.finditer(line)]
    matches: list[str] = []
    windows_matches = list(_WINDOWS_ABSOLUTE_PATH.finditer(line))
    for match in windows_matches:
        if _span_is_ignored(match.start(), ignored_spans, line):
            continue
        if any(start <= match.start() < end for start, end in url_spans):
            continue
        if (
            match.group(0).startswith("//")
            and _assignment_key_tokens(line, match.start()) & _WEB_RESOURCE_KEY_TOKENS
        ):
            continue
        matches.append(match.group(0))
    for match in _POSIX_ABSOLUTE_PATH.finditer(line):
        match_start = match.start("value")
        if _span_is_ignored(match_start, ignored_spans, line):
            continue
        if any(candidate.start() <= match_start < candidate.end() for candidate in windows_matches):
            continue
        prefix = line[:match_start].rstrip("'\"")
        if _HTTP_ROUTE_CONTEXT.search(prefix):
            continue
        key_tokens = _assignment_key_tokens(line, match_start)
        if key_tokens & _WEB_RESOURCE_KEY_TOKENS or key_tokens in ({"pattern"}, {"regex"}):
            continue
        value = match.group("value")
        if not _posix_candidate_is_local_path(line, match_start, value):
            continue
        matches.append(value)
    return matches


def _path_finding_is_audited(path: str, matched: str) -> bool:
    if _is_audited_absolute_path_example(path, matched):
        return True
    return any(
        _is_audited_absolute_path_example(path, candidate)
        for candidate in _absolute_local_path_matches(matched)
    )


def _scan_plain_text(
    relative_path: str,
    text: str,
    *,
    credential_ignored_spans: dict[int, list[tuple[int, int]]] | None = None,
    path_ignored_spans: dict[int, list[tuple[int, int]]] | None = None,
) -> list[ReleaseFinding]:
    path = normalize_archive_path(relative_path)
    findings: list[ReleaseFinding] = []
    cli_literal_spans = _python_cli_literal_spans(text) if path.casefold().endswith(".py") else {}
    credential_ignored_spans = credential_ignored_spans or {}
    path_ignored_spans = path_ignored_spans or {}
    patterns = (
        (
            "private_key",
            re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
            "private key material",
        ),
        (
            "credential_assignment",
            re.compile(
                r"(?ix)(?<![A-Za-z0-9_])['\"]?"
                r"(?:(?:feishu|lark|openai|azure|aws|google|oauth|auto_cut|audio_sound)_)*"
                r"(?:access[_-]?token|refresh[_-]?token|api[_-]?key|app[_-]?secret|"
                r"client[_-]?secret|authorization[_-]?url|credential|device[_-]?code|"
                r"password|token)\b['\"]?\s*[:=]\s*(?:Bearer\s+)?"
                r"(?:\"[^\"\r\n]{6,}\"|'[^'\r\n]{6,}'|"
                r"[A-Za-z0-9][A-Za-z0-9._~+/=?&:%-]{5,})"
                r"(?=$|[]\s,;#'\"})])"
            ),
            "credential-like assignment",
        ),
        (
            "credential_assignment",
            _GENERIC_CREDENTIAL_LITERAL_ASSIGNMENT,
            "credential-like assignment",
        ),
        (
            "bearer_token",
            re.compile(r"(?i)\b" + "Bea" + r"rer\s+[A-Za-z0-9._~+/=-]{12,}"),
            "authorization header credential",
        ),
        (
            "feishu_destination",
            re.compile(r"\b(?:oc|ou|on|ocg)_[A-Za-z0-9_-]{16,}\b"),
            "Feishu destination identifier",
        ),
        (
            "user_profile_path",
            re.compile(r"(?i)\b[A-Z]:(?:\\+|/+)Users(?:\\+|/+)[^\\/\s'\"]+"),
            "Windows user-profile path",
        ),
        (
            "build_repository_path",
            re.compile(
                r"(?i)\bD:(?:\\+|/+)codex(?:\\+|/+)"
                r"(?:Auto-Cut-CopyA|Audio-sound-release-clean-v0\.1\.0)\b"
            ),
            "build repository path",
        ),
        (
            "absolute_project_binding",
            re.compile(
                r"(?i)['\"](?:project_path|project_root|draft_path|draft_root|registry_root|"
                r"profile_path|asset_path|scale_reference_path|approved_preview_path|"
                r"source_folder)['\"]\s*:\s*"
                r"['\"][A-Z]:(?:\\+|/+)[^'\"]*['\"]"
            ),
            "absolute project binding",
        ),
    )
    for line_number, line in enumerate(text.splitlines(), start=1):
        for code, pattern, summary in patterns:
            for match in pattern.finditer(line):
                matched = match.group(0)
                if code == "credential_assignment" and _span_is_ignored(
                    match.start(), credential_ignored_spans.get(line_number, ()), line
                ):
                    continue
                if code in _PATH_FINDING_CODES:
                    if _path_finding_is_audited(path, matched):
                        continue
                elif code == "credential_assignment" and _is_audited_nonsecret_credential_value(
                    path, matched
                ):
                    continue
                elif _looks_like_nonpath_placeholder(path, matched):
                    continue
                findings.append(
                    ReleaseFinding(
                        code=code,
                        path=path,
                        line=line_number,
                        summary=summary,
                    )
                )
                break
        existing_path_finding = any(
            finding.line == line_number
            and finding.code
            in {"user_profile_path", "build_repository_path", "absolute_project_binding"}
            for finding in findings
        )
        if not existing_path_finding:
            for matched in _absolute_local_path_matches(
                line,
                ignored_spans=(
                    *cli_literal_spans.get(line_number, ()),
                    *path_ignored_spans.get(line_number, ()),
                ),
            ):
                if _is_audited_absolute_path_example(path, matched):
                    continue
                findings.append(
                    ReleaseFinding(
                        code="absolute_local_path",
                        path=path,
                        line=line_number,
                        summary="local absolute path or machine binding",
                    )
                )
                break
    return findings


def _static_literal_value(node: ast.AST) -> str | bytes | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_literal_value(node.left)
        right = _static_literal_value(node.right)
        if left is not None and type(left) is type(right):
            return left + right
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for part in node.values:
            if not isinstance(part, ast.Constant) or not isinstance(part.value, str):
                return None
            parts.append(part.value)
        return "".join(parts)
    return None


def _static_string_value(node: ast.AST) -> str | None:
    value = _static_literal_value(node)
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


def _assignment_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        return [node.attr]
    if isinstance(node, ast.Starred):
        return _assignment_names(node.value)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [name for element in node.elts for name in _assignment_names(element)]
    return []


def _enclosing_call_argument(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> tuple[ast.Call, int] | None:
    current = node
    parent = parents.get(current)
    while isinstance(parent, ast.JoinedStr):
        current = parent
        parent = parents.get(current)
    if not isinstance(parent, ast.Call) or current not in parent.args:
        return None
    return parent, parent.args.index(current)


def _is_regex_pattern_argument(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    call_argument = _enclosing_call_argument(node, parents)
    if call_argument is None:
        return False
    call, argument_index = call_argument
    function = call.func
    if not isinstance(function, ast.Attribute):
        return False
    if (
        isinstance(function.value, ast.Name)
        and function.value.id == "re"
        and function.attr
        in {
            "compile",
            "findall",
            "finditer",
            "fullmatch",
            "match",
            "search",
            "split",
            "sub",
            "subn",
        }
    ):
        return argument_index == 0
    return function.attr in {"assertRegex", "assertNotRegex"} and argument_index == 1


def _python_value_contexts(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> list[str]:
    contexts: list[str] = []
    current = node
    parent = parents.get(current)
    while parent is not None:
        if isinstance(parent, ast.Dict):
            if current not in parent.values:
                break
            index = parent.values.index(current)
            key = _static_string_value(parent.keys[index])
            if key is not None:
                contexts.append(key)
        elif isinstance(parent, ast.Assign) and current is parent.value:
            contexts.extend(name for target in parent.targets for name in _assignment_names(target))
            break
        elif isinstance(parent, (ast.AnnAssign, ast.NamedExpr)) and current is parent.value:
            contexts.extend(_assignment_names(parent.target))
            break
        elif isinstance(parent, ast.keyword) and current is parent.value:
            if parent.arg:
                contexts.append(parent.arg)
            break
        elif not isinstance(
            parent,
            (
                ast.BinOp,
                ast.BoolOp,
                ast.FormattedValue,
                ast.IfExp,
                ast.JoinedStr,
                ast.List,
                ast.Set,
                ast.Tuple,
            ),
        ):
            break
        current = parent
        parent = parents.get(current)
    return contexts


def _python_static_string_candidates(
    text: str, *, allow_audited_negative_assertions: bool = False
) -> list[tuple[int, str]] | None:
    try:
        tree = ast.parse(text)
    except (RecursionError, SyntaxError, ValueError):
        return None
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    parser_names = _argparse_parser_names(tree)
    candidates: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        value = _static_string_value(node)
        if value is None:
            continue
        parent = parents.get(node)
        if parent is not None and _static_string_value(parent) is not None:
            continue
        if _is_regex_pattern_argument(node, parents):
            continue
        parent = parents.get(node)
        if (
            allow_audited_negative_assertions
            and isinstance(parent, ast.Compare)
            and parent.left is node
            and parent.ops
            and isinstance(parent.ops[0], ast.NotIn)
            and isinstance(parents.get(parent), ast.Assert)
            and _AUDITED_BUILD_ROOT_ASSERTION.fullmatch(value)
        ):
            continue
        call_argument = _enclosing_call_argument(node, parents)
        if call_argument is not None and _is_argparse_option_argument(
            call_argument[0], call_argument[1], parser_names
        ):
            continue

        contexts = _python_value_contexts(node, parents)
        candidates.extend(
            (
                node.lineno,
                f"{'token' if _is_credential_name(context) else context} = {value!r}",
            )
            for context in contexts
        )
        if not contexts:
            candidates.append((node.lineno, repr(value)))
    return candidates


class _JsonObjectPairs(list[tuple[object, object]]):
    pass


def _json_string_candidates(text: str) -> list[str] | None:
    try:
        payload = json.loads(text, object_pairs_hook=_JsonObjectPairs)
    except (json.JSONDecodeError, RecursionError):
        return None

    candidates: list[str] = []

    def walk(value: object, contexts: tuple[str, ...] = ()) -> None:
        if isinstance(value, _JsonObjectPairs):
            for key, nested in value:
                nested_contexts = contexts
                if isinstance(key, str):
                    nested_contexts = (*contexts, key)
                walk(nested, nested_contexts)
        elif isinstance(value, list):
            for nested in value:
                walk(nested, contexts)
        elif isinstance(value, str):
            if contexts:
                candidates.extend(
                    f"{'token' if _is_credential_name(context) else context!r}: {value!r}"
                    for context in contexts
                )
            else:
                candidates.append(repr(value))

    try:
        walk(payload)
    except RecursionError:
        return None
    return candidates


def scan_text(relative_path: str, text: str) -> list[ReleaseFinding]:
    path = normalize_archive_path(relative_path)
    credential_ignored_spans: dict[int, list[tuple[int, int]]] = {}
    rejected_credential_lines: set[int] = set()
    path_ignored_spans: dict[int, list[tuple[int, int]]] = {}
    rejected_add_argument_path_lines: set[int] = set()
    if path.casefold().endswith(".py"):
        credential_ignored_spans, rejected_credential_lines = _python_credential_passthrough_spans(
            path, text
        )
        rejected_add_argument_path_lines = _python_add_argument_path_lines(text)
        if PurePosixPath(path).parts[:1] == ("tests",):
            path_ignored_spans = _python_negative_assertion_literal_spans(text)
    findings = _scan_plain_text(
        path,
        text,
        credential_ignored_spans=credential_ignored_spans,
        path_ignored_spans=path_ignored_spans,
    )
    findings.extend(
        ReleaseFinding(
            code="credential_assignment",
            path=path,
            line=line,
            summary="credential container does not have an approved runtime source",
        )
        for line in sorted(rejected_credential_lines)
    )
    findings.extend(
        ReleaseFinding(
            code="absolute_local_path",
            path=path,
            line=line,
            summary="path-like add_argument literal is not an argparse option",
        )
        for line in sorted(rejected_add_argument_path_lines)
    )
    supplemental: list[tuple[int | None, str]] = []
    parse_failed = False
    if path.casefold().endswith(".py"):
        candidates = _python_static_string_candidates(
            text,
            allow_audited_negative_assertions=(PurePosixPath(path).parts[:1] == ("tests",)),
        )
        if candidates is None:
            parse_failed = True
        else:
            supplemental.extend(candidates)
    elif path.casefold().endswith(".json"):
        candidates = _json_string_candidates(text)
        if candidates is None:
            parse_failed = True
        else:
            supplemental.extend((None, candidate) for candidate in candidates)

    if parse_failed:
        findings.append(
            ReleaseFinding(
                code="unscannable_content",
                path=path,
                line=None,
                summary="structured source could not be parsed safely",
            )
        )

    for source_line, candidate in supplemental:
        for finding in _scan_plain_text(path, candidate):
            findings.append(
                ReleaseFinding(
                    code=finding.code,
                    path=finding.path,
                    line=source_line,
                    summary=finding.summary,
                )
            )

    unique: list[ReleaseFinding] = []
    seen: set[tuple[str, str, int | None, str]] = set()
    for finding in findings:
        key = (finding.code, finding.path, finding.line, finding.summary)
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return unique


def _is_binary_asset_location(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(parts) and (
        parts[0] == "assets"
        or parts[:2] == ("tests", "fixtures")
        or parts[0] in {"docs", "skills"}
        and "assets" in parts[1:]
    )


def _is_approved_binary_asset(path: str, data: bytes) -> bool:
    if not _is_binary_asset_location(path):
        return False
    digest = hashlib.sha256(data).hexdigest()
    if digest not in _AUDITED_BINARY_ASSET_DIGESTS:
        return False
    suffix = PurePosixPath(path).suffix.casefold()
    if suffix in {".jpg", ".jpeg", ".png"}:
        return data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"))
    if suffix == ".mp3":
        return data.startswith(b"ID3") or (
            len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0
        )
    if suffix in {".mp4", ".mov"}:
        return len(data) >= 12 and data[4:8] == b"ftyp"
    if suffix == ".wav":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WAVE"
    if path.startswith("assets/artistEffect/"):
        return suffix in {
            ".material",
            ".meta",
            ".prefab",
            ".rt",
            ".scene",
            ".texture",
            ".xshader",
        }
    return False


def _decoded_text_is_scannable(text: str) -> bool:
    if "\x00" in text:
        return False
    if not text:
        return True
    readable = sum(character.isprintable() or character in "\r\n\t" for character in text)
    return readable / len(text) >= 0.85


def _decode_scannable_text(data: bytes) -> str | None:
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        try:
            decoded = data.decode("utf-32")
        except UnicodeDecodeError:
            return None
        return decoded if _decoded_text_is_scannable(decoded) else None
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            decoded = data.decode("utf-16")
        except UnicodeDecodeError:
            return None
        return decoded if _decoded_text_is_scannable(decoded) else None
    try:
        decoded = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        decoded = ""
    else:
        if _decoded_text_is_scannable(decoded):
            return decoded
        if "\x00" not in decoded:
            return None

    if len(data) < 4 or len(data) % 2:
        return None

    candidates: list[tuple[float, int, str]] = []
    for encoding in ("utf-16-le", "utf-16-be"):
        try:
            candidate = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if not candidate or not _decoded_text_is_scannable(candidate):
            continue
        readable = sum(character.isprintable() or character in "\r\n\t" for character in candidate)
        ascii_evidence = sum(
            ord(character) < 128 and (character.isprintable() or character in "\r\n\t")
            for character in candidate
        )
        readable_ratio = readable / len(candidate)
        if readable_ratio >= 0.85 and ascii_evidence >= 4:
            candidates.append((readable_ratio, ascii_evidence, candidate))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def scan_release_tree(root: Path, release_paths: Iterable[str]) -> list[ReleaseFinding]:
    findings: list[ReleaseFinding] = []
    for raw_path in release_paths:
        path = normalize_archive_path(raw_path)
        if is_forbidden_path(path):
            findings.append(
                ReleaseFinding("forbidden_path", path, None, "machine-owned or runtime path")
            )
            continue
        source = root / Path(*PurePosixPath(path).parts)
        if source.is_symlink():
            findings.append(ReleaseFinding("symlink", path, None, "symbolic link"))
            continue
        if not source.is_file():
            findings.append(ReleaseFinding("missing_file", path, None, "release file is missing"))
            continue
        data = source.read_bytes()
        decoded = _decode_scannable_text(data)
        if decoded is None:
            if not _is_approved_binary_asset(path, data):
                findings.append(
                    ReleaseFinding(
                        "unscannable_content",
                        path,
                        None,
                        "content is neither decodable text nor an approved binary asset",
                    )
                )
            continue
        findings.extend(scan_text(path, decoded))
    return findings
