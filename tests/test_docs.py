from __future__ import annotations

import json
import re
import unittest

from helpers import ROOT

SKILLS = {
    "orchestrate": "opencode-orchestrator-orchestrate",
    "workflow": "opencode-orchestrator-workflow",
    "report": "opencode-orchestrator-report",
}


class DocsContractTests(unittest.TestCase):
    def test_skill_frontmatter(self) -> None:
        for folder, name in SKILLS.items():
            text = (ROOT / "skills" / folder / "SKILL.md").read_text(encoding="utf-8")
            self.assertTrue(text.startswith("---\n"), folder)
            self.assertIn(f"name: {name}\n", text)
            self.assertRegex(text, r"\ndescription: \S")

    def test_plugin_paths_exist(self) -> None:
        pattern = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([\w./-]+)")
        for path in list((ROOT / "skills").rglob("*.md")) + [ROOT / "README.md"]:
            for relative in pattern.findall(path.read_text(encoding="utf-8")):
                self.assertTrue((ROOT / relative).exists(), f"{path}: {relative}")

    def test_reference_map_matches_files(self) -> None:
        text = (ROOT / "skills" / "orchestrate" / "SKILL.md").read_text(encoding="utf-8")
        listed = set(re.findall(r"`references/([\w-]+\.md)`", text))
        present = {p.name for p in (ROOT / "skills" / "orchestrate" / "references").glob("*.md")}
        self.assertEqual(listed, present)

    def test_no_codex_leftovers(self) -> None:
        for path in list((ROOT / "skills").rglob("*.md")) + [ROOT / "docs" / "orchestration-contract.md"]:
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(?i)codex exec|\.codex-orchestrator|codex_orch", str(path))

    def test_contract_entry_examples_parse(self) -> None:
        text = (ROOT / "docs" / "orchestration-contract.md").read_text(encoding="utf-8")
        blocks = re.findall(r"```jsonl\n(.*?)```", text, re.S)
        types = {json.loads(line)["type"] for block in blocks for line in block.splitlines() if line}
        self.assertEqual(
            types,
            {"run_started", "task", "execution", "execution_result", "verification", "decision",
             "run_closed"},
        )

    def test_versions_agree(self) -> None:
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
        pyproject = (ROOT / "pyproject.toml").read_text()
        self.assertEqual(plugin["version"], market["plugins"][0]["version"])
        self.assertIn(f'version = "{plugin["version"]}"', pyproject)

    def test_handoff_block_lists_all_headings(self) -> None:
        from opencode_orchestrator.launch import HANDOFF_HEADINGS

        block = (ROOT / "skills" / "orchestrate" / "references" / "handoff-prompt.md").read_text()
        for heading in HANDOFF_HEADINGS:
            self.assertIn(f"\n{heading}\n", block)




class HandoffBlockSyncTests(unittest.TestCase):
    def test_launcher_fix_prompt_block_matches_docs(self) -> None:
        from opencode_orchestrator.launch import HANDOFF_BLOCK

        doc = (ROOT / "skills" / "orchestrate" / "references" / "handoff-prompt.md").read_text()
        block = doc.split("```markdown\n", 1)[1].split("\n```", 1)[0]
        self.assertEqual(block, HANDOFF_BLOCK)


if __name__ == "__main__":
    unittest.main()
