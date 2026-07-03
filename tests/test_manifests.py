"""Tests for plugin/marketplace manifest files (Claude + Codex).

Validates: all manifest JSON files parse; the two plugin manifests
(.claude-plugin/plugin.json, .codex-plugin/plugin.json) stay in lockstep on
version; the Codex plugin manifest has the required fields with correct
shapes; the Codex marketplace manifest points at this repo via a
`./`-prefixed local source path.
"""
import json
import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

CLAUDE_PLUGIN_MANIFEST = REPO_ROOT / ".claude-plugin" / "plugin.json"
CLAUDE_MARKETPLACE_MANIFEST = REPO_ROOT / ".claude-plugin" / "marketplace.json"
CODEX_PLUGIN_MANIFEST = REPO_ROOT / ".codex-plugin" / "plugin.json"
CODEX_MARKETPLACE_MANIFEST = REPO_ROOT / ".agents" / "plugins" / "marketplace.json"

HOOKS_DIR = REPO_ROOT / "hooks"
CLAUDE_HOOKS_MANIFEST = HOOKS_DIR / "hooks.json"
CODEX_HOOKS_MANIFEST = HOOKS_DIR / "codex-hooks.json"

KEBAB_CASE_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class ManifestJsonValidityTests(unittest.TestCase):
    def test_claude_plugin_manifest_parses_as_json(self):
        self.assertIsInstance(_load_json(CLAUDE_PLUGIN_MANIFEST), dict)

    def test_claude_marketplace_manifest_parses_as_json(self):
        self.assertIsInstance(_load_json(CLAUDE_MARKETPLACE_MANIFEST), dict)

    def test_codex_plugin_manifest_parses_as_json(self):
        self.assertIsInstance(_load_json(CODEX_PLUGIN_MANIFEST), dict)

    def test_codex_marketplace_manifest_parses_as_json(self):
        self.assertIsInstance(_load_json(CODEX_MARKETPLACE_MANIFEST), dict)


class PluginVersionLockstepTests(unittest.TestCase):
    def test_plugin_versions_match_across_claude_and_codex(self):
        claude = _load_json(CLAUDE_PLUGIN_MANIFEST)
        codex = _load_json(CODEX_PLUGIN_MANIFEST)
        self.assertEqual(claude["version"], codex["version"])


class CodexPluginManifestShapeTests(unittest.TestCase):
    def setUp(self):
        self.manifest = _load_json(CODEX_PLUGIN_MANIFEST)

    def test_has_required_fields(self):
        for field in ("name", "version", "description"):
            self.assertIn(field, self.manifest)

    def test_name_is_kebab_case(self):
        self.assertRegex(self.manifest["name"], KEBAB_CASE_RE)

    def test_version_is_semver(self):
        self.assertRegex(self.manifest["version"], SEMVER_RE)

    def test_description_matches_claude_plugin_manifest(self):
        claude = _load_json(CLAUDE_PLUGIN_MANIFEST)
        self.assertEqual(self.manifest["description"], claude["description"])


class CodexMarketplaceManifestShapeTests(unittest.TestCase):
    def setUp(self):
        self.manifest = _load_json(CODEX_MARKETPLACE_MANIFEST)

    def test_has_name_and_display_name(self):
        self.assertIn("name", self.manifest)
        self.assertIn("interface", self.manifest)
        self.assertIn("displayName", self.manifest["interface"])

    def test_has_plugins_list_with_local_source(self):
        self.assertIn("plugins", self.manifest)
        self.assertTrue(len(self.manifest["plugins"]) >= 1)
        plugin = self.manifest["plugins"][0]
        self.assertEqual(plugin["name"], "theforge")
        self.assertIn("source", plugin)
        self.assertEqual(plugin["source"]["source"], "local")

    def test_source_path_is_dot_slash_prefixed(self):
        plugin = self.manifest["plugins"][0]
        self.assertTrue(plugin["source"]["path"].startswith("./"))


def _session_start_commands(hook_config):
    """Flatten every command string wired to SessionStart in a hooks.json dict."""
    commands = []
    for matcher_group in hook_config.get("hooks", {}).get("SessionStart", []):
        for hook in matcher_group.get("hooks", []):
            if "command" in hook:
                commands.append(hook["command"])
    return commands


class HookConfigTests(unittest.TestCase):
    def test_all_hook_json_files_parse(self):
        hook_json_files = sorted(HOOKS_DIR.glob("*.json"))
        self.assertTrue(len(hook_json_files) >= 1)
        for path in hook_json_files:
            self.assertIsInstance(_load_json(path), dict)

    def test_claude_hooks_json_wires_session_start_to_session_start_script(self):
        config = _load_json(CLAUDE_HOOKS_MANIFEST)
        commands = _session_start_commands(config)
        self.assertTrue(
            any("hooks/session-start" in command for command in commands),
            f"expected a SessionStart command referencing hooks/session-start, got {commands}",
        )

    def test_codex_hooks_json_references_same_session_start_script_if_present(self):
        if not CODEX_HOOKS_MANIFEST.exists():
            self.skipTest("hooks/codex-hooks.json not present (shared hooks.json outcome)")
        config = _load_json(CODEX_HOOKS_MANIFEST)
        commands = _session_start_commands(config)
        self.assertTrue(
            any("hooks/session-start" in command for command in commands),
            f"expected a SessionStart command referencing hooks/session-start, got {commands}",
        )


if __name__ == "__main__":
    unittest.main()
