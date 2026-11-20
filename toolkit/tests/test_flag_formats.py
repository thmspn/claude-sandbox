"""Contest-neutral candidate ranking and honest cipher-guess provenance."""
from __future__ import annotations

import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules import discover, go


class FlagFormats(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mods = discover()

    def runner(self):
        return go.Runner(self.mods, 5, 30, False, out=lambda s: None)

    def test_all_prefixes_equal_after_base64(self):
        for prefix in ('picoCTF', 'flag', 'CTF', 'cybersci', 'CyberSci', 'HTB', 'MapleCTF', 'x', 'new_event-2026', 'A' * 64):
            with self.subTest(prefix=prefix):
                flag = prefix + '{novice_b64_ok}'
                r = self.runner()
                go.chain_text(r, base64.b64encode(flag.encode()).decode())
                ranked = go.rank(r.cands)
                self.assertEqual((ranked[0][0], ranked[0][1].text), (113, flag))
                self.assertTrue(all(score <= 13 for score, c in ranked if c.text != flag))
                self.assertNotIn('word{...}', r.cands)

    def test_plain_flags_and_sentence_wrappers_are_not_lost(self):
        for text in ('FreshEvent{a}', 'The answer is FreshEvent{hello world} in this sentence.'):
            r = self.runner()
            go.chain_text(r, text)
            flag = go.TOKEN_RE.search(text).group()
            self.assertGreaterEqual(dict((c.text, score) for score, c in go.rank(r.cands))[flag], 107)
            self.assertFalse(r.cands[flag].speculative_only)
            self.assertNotIn('word{...}', r.cands)

    def test_embedded_encoded_blob_still_requires_token_only(self):
        # Do not silently expand a scoring fix into an embedded-token decoder.
        for prefix in ('flag', 'CTF', 'cybersci', 'CyberSci', 'HTB', 'FreshEvent'):
            r = self.runner()
            flag = prefix + '{novice_b64_ok}'
            token = base64.b64encode(flag.encode()).decode()
            go.chain_text(r, 'We intercepted ' + token + ' please decode it.')
            self.assertNotIn(flag, r.cands)
            self.assertIn(token, r.text_tokens)
            self.assertIn('ONLY that', '\n'.join(go._next_step_hint(r)))

    def test_known_prefix_not_a_backdoor_for_speculative_guess(self):
        r = self.runner()
        r.add_text('flag{random_noise}', 'cipher', speculative=True)
        self.assertEqual(go.rank(r.cands)[0][0], 13)
        r.add_text('flag{random_noise}', 'actual result')
        self.assertEqual(go.rank(r.cands)[0][0], 118)
        self.assertFalse(r.cands['flag{random_noise}'].speculative_only)

    def test_unknown_decoder_section_cannot_supply_strong_evidence(self):
        r = self.runner()
        r.add_decode_output('== future section ==\nflag{example}', 'decode')
        self.assertEqual(r.cands, {})

    def test_byte_and_wide_scan_and_bounds(self):
        for data in (b'\xffNew-Event{hello world}\x00', 'x{ok}'.encode('utf-16-le')):
            r = self.runner()
            r.add_bytes(data, 'raw')
            self.assertTrue(r.cands)
            self.assertGreaterEqual(go.rank(r.cands)[0][0], 107)
        for bad in ('P' * 65 + '{answer}', 'P{a\nb}', 'P{{nested}}', 'P{}'):
            self.assertIsNone(go.TOKEN_RE.search(bad))

    def test_charset_and_corroboration_tiebreakers(self):
        r = self.runner()
        r.add_text('New{abc_123}', 'a')
        r.add_text('New{abc_123}', 'a')
        self.assertEqual(go.rank(r.cands)[0][0], 113)
        r.add_text('New{abc_123}', 'b')
        self.assertEqual(go.rank(r.cands)[0][0], 118)
        r.add_text('New{hello world}', 'a')
        self.assertEqual(dict((c.text, score) for score, c in go.rank(r.cands))['New{hello world}'], 107)


if __name__ == '__main__':
    unittest.main()
