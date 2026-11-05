import os
os.environ.setdefault("HUBMEMORY_SKIP_INTEGRATION", "1")
import unittest
import install_agent_hooks as m

B = "BLOCK"


class MergeInstructions(unittest.TestCase):
    def test_append_to_empty_and_existing(self):
        self.assertEqual(m.merge_instructions("", m.BEGIN + "\nx\n" + m.END), m.BEGIN + "\nx\n" + m.END + "\n")
        out = m.merge_instructions("# Mine\nkeep\n", "B")
        self.assertTrue(out.startswith("# Mine\nkeep\n") and out.rstrip().endswith("B"))

    def test_replace_managed_block_only(self):
        block = m.BEGIN + "\nnew\n" + m.END
        text = "a\n" + m.BEGIN + "\nold\n" + m.END + "\nz\n"
        out = m.merge_instructions(text, block)
        self.assertEqual(out, "a\n" + block + "\nz\n")
        self.assertEqual(m.merge_instructions(out, block), out)

    def test_migrate_legacy_section_keeps_neighbours(self):
        text = "# G\n\n## Other\nmine\n\n" + m.LEGACY_HEADING + "\nold\n### sub\nold2\n\n## After\nkeep\n"
        out = m.merge_instructions(text, "NEW")
        self.assertIn("## Other\nmine", out)
        self.assertIn("NEW\n\n## After\nkeep", out)
        self.assertNotIn("old2", out)

    def test_rendered_templates_have_no_placeholders(self):
        for agent in ("dsh", "codex"):
            block = m.render_block(agent)
            self.assertNotIn("{PY}", block)
            self.assertNotIn("{SKILL}", block)
            self.assertIn("本回合", block)


if __name__ == "__main__":
    unittest.main()
