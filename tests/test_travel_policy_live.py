import os
import unittest


@unittest.skipUnless(os.environ.get("RUN_LIVE_POLICY_CHECKS") == "1",
                     "Live embedding checks require RUN_LIVE_POLICY_CHECKS=1")
class LivePolicyTests(unittest.TestCase):
    def test_real_embedding_shape(self):
        from travel_agent.policy.embeddings import embed_text

        vector = embed_text("What expense receipts are needed?", "RETRIEVAL_QUERY")
        self.assertEqual(len(vector), 768)
        self.assertAlmostEqual(sum(value * value for value in vector), 1, places=5)
