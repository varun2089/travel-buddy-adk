import json
import os
import unittest
from unittest.mock import patch

from google import genai
from google.adk.models.google_llm import Gemini
from google.genai import types

from travel_agent.agent import root_agent
from travel_agent.policy.embeddings import embed_text


class BackendConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_installed_sdk_serializes_vertex_embedding_requests_without_network(self):
        vector = [1.0] + [0.0] * 767
        response = types.HttpResponse(headers={}, body=json.dumps({
            "predictions": [{"embeddings": {"values": vector}}]}))
        with patch.dict(os.environ, {"GOOGLE_API_KEY": "offline-placeholder",
                                    "GOOGLE_GENAI_USE_VERTEXAI": "false"}, clear=True):
            with genai.Client(vertexai=True, project="gen-lang-client-0842015305",
                              location="us-central1") as client, patch.object(
                client._api_client, "request", return_value=response) as request:
                self.assertIsNone(client._api_client.api_key)
                self.assertIs(client._api_client.vertexai, True)
                self.assertEqual(client._api_client.project, "gen-lang-client-0842015305")
                self.assertEqual(client._api_client.location, "us-central1")
                for task in ("RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"):
                    result = client.models.embed_content(model="gemini-embedding-001", contents="Keep receipts",
                        config=types.EmbedContentConfig(task_type=task, output_dimensionality=768))
                    self.assertEqual(result.embeddings[0].values, vector)
                    method, path, body, options = request.call_args.args
                    self.assertEqual(method, "post")
                    self.assertEqual(path, "publishers/google/models/gemini-embedding-001:predict")
                    self.assertEqual(body["instances"], [{"content": "Keep receipts", "task_type": task}])
                    self.assertEqual(body["parameters"]["outputDimensionality"], 768)

    async def test_generation_settings_override_shared_backend_without_mutating_environment(self):
        environment = {
            "GOOGLE_GENAI_USE_VERTEXAI": "false",
            "GOOGLE_GENAI_USE_ENTERPRISE": "false",
            "GOOGLE_CLOUD_PROJECT": "embedding-project",
            "GOOGLE_CLOUD_LOCATION": "us-central1",
            "GOOGLE_API_KEY": "offline-placeholder",
        }
        with patch.dict(os.environ, environment, clear=True), patch("google.genai.Client") as constructor:
            self.assertIsInstance(root_agent.model, Gemini)
            self.assertEqual(root_agent.model.model, "gemini-3.8-flash")
            model = root_agent.model.model_copy()
            self.assertIs(model.api_client, constructor.return_value)
            arguments = constructor.call_args.kwargs
            self.assertIs(arguments["vertexai"], True)
            self.assertEqual(arguments["project"], "gen-lang-client-0842015305")
            self.assertEqual(arguments["location"], "global")
            self.assertNotIn("api_key", arguments)
            self.assertNotIn("credentials", arguments)
            self.assertEqual(dict(os.environ), environment)

    async def test_embedding_vertex_settings_are_independent_and_preserve_vector_configuration(self):
        vector = [1.0] + [0.0] * 767
        for backend in ("false", "true"):
            environment = {"GOOGLE_GENAI_USE_VERTEXAI": backend,
                           "GOOGLE_CLOUD_PROJECT": "embedding-project",
                           "GOOGLE_CLOUD_LOCATION": "global",
                           "GOOGLE_API_KEY": "offline-placeholder"}
            with self.subTest(backend=backend), patch.dict(os.environ, environment, clear=True), patch(
                "google.genai.Client") as constructor:
                client = constructor.return_value.__enter__.return_value
                client.models.embed_content.return_value = types.EmbedContentResponse(
                    embeddings=[types.ContentEmbedding(values=vector)])
                for task in ("RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"):
                    self.assertEqual(embed_text("Keep receipts", task), vector)
                    settings = constructor.call_args.kwargs
                    self.assertEqual(set(settings), {"vertexai", "project", "location", "http_options"})
                    self.assertIs(settings["vertexai"], True)
                    self.assertEqual(settings["project"], "gen-lang-client-0842015305")
                    self.assertEqual(settings["location"], "us-central1")
                    arguments = client.models.embed_content.call_args.kwargs
                    self.assertEqual(arguments["model"], "gemini-embedding-001")
                    self.assertEqual(arguments["config"].output_dimensionality, 768)
                    self.assertEqual(arguments["config"].task_type, task)
                self.assertEqual(dict(os.environ), environment)
