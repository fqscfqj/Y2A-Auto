import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from modules import content_moderator as moderator

try:
    from alibabacloud_green20220302 import models as sdk_models
except ImportError:
    sdk_models = None


class FakeModel:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeOssBucket:
    uploads = []

    def __init__(self, auth, endpoint, bucket):
        self.endpoint = endpoint
        self.bucket = bucket

    def put_object_from_file(self, object_name, path):
        self.uploads.append((self.endpoint, self.bucket, object_name, path))


class FakeGreenClient:
    def __init__(self, response=None):
        self.response = response or SimpleNamespace(status_code=200, body=SimpleNamespace(code=200, data=SimpleNamespace(result=[{"Label": "normal", "Suggestion": "pass"}])))
        self.request = None

    def describe_upload_token(self):
        token = SimpleNamespace(oss_internet_end_point="oss.example", bucket_name="moderation", access_key_id="id", access_key_secret="secret", security_token="sts", file_name_prefix="tmp/")
        return SimpleNamespace(status_code=200, body=SimpleNamespace(code=200, data=token))

    def image_moderation_with_options(self, request, runtime):
        self.request = request
        return self.response


class ImageModerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        self.temp.write(b"image")
        self.temp.close()
        self.addCleanup(lambda: os.path.exists(self.temp.name) and os.unlink(self.temp.name))
        FakeOssBucket.uploads.clear()
        self.patches = [
            patch.object(moderator, "ALIBABA_CLOUD_AVAILABLE", True),
            patch.object(moderator, "OSS_AVAILABLE", True),
            patch.object(moderator, "oss2", SimpleNamespace(StsAuth=lambda *args: args, Bucket=FakeOssBucket)),
            patch.object(moderator, "models", SimpleNamespace(ImageModerationRequest=FakeModel)),
            patch.object(moderator, "RuntimeOptions", lambda: object()),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def make_moderator(self, client=None, config=None):
        instance = moderator.AlibabaCloudModerator(config or {})
        instance.client = client or FakeGreenClient()
        return instance

    def test_uploads_local_file_then_uses_oss_reference(self):
        client = FakeGreenClient()
        result = self.make_moderator(client).moderate_image(self.temp.name)
        self.assertTrue(result["pass"])
        self.assertEqual(result["status"], "passed")
        self.assertEqual(FakeOssBucket.uploads[0][1], "moderation")
        self.assertEqual(FakeOssBucket.uploads[0][3], self.temp.name)
        self.assertNotIn("imageUrl", json.loads(client.request.service_parameters))
        self.assertEqual(client.request.service, "baselineCheck")
        self.assertTrue(json.loads(client.request.service_parameters)["ossObjectName"].startswith("tmp/"))

    @unittest.skipIf(sdk_models is None, "Alibaba SDK is not installed")
    def test_real_sdk_upload_token_uses_internet_end_point(self):
        client = FakeGreenClient()
        token_body = sdk_models.DescribeUploadTokenResponseBody().from_map({
            "Code": 200,
            "Data": {
                "AccessKeyId": "id", "AccessKeySecret": "secret",
                "SecurityToken": "sts", "BucketName": "moderation",
                "FileNamePrefix": "tmp/",
                "OssInternetEndPoint": "https://oss.example",
            },
        })
        client.describe_upload_token = lambda: sdk_models.DescribeUploadTokenResponse(
            status_code=200, body=token_body)
        with patch.object(moderator, "models", sdk_models):
            result = self.make_moderator(client).moderate_image(self.temp.name)
        self.assertTrue(result["pass"])
        self.assertEqual(FakeOssBucket.uploads[0][0], "https://oss.example")
        self.assertIsInstance(client.request, sdk_models.ImageModerationRequest)

    def test_custom_service_and_risk_response(self):
        body = SimpleNamespace(code=200, data=SimpleNamespace(result=[{"Label": "porn", "Suggestion": "block", "Confidence": 99}]))
        client = FakeGreenClient(SimpleNamespace(status_code=200, body=body))
        result = self.make_moderator(client).moderate_image(self.temp.name, "customService")
        self.assertFalse(result["pass"])
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(client.request.service, "customService")
        self.assertEqual(result["details"][0]["suggestion"], "block")

    def test_review_unknown_label_and_inconsistent_pass_fail_closed(self):
        cases = [
            ({"Label": "normal", "Suggestion": "review"}, "review"),
            ({"Label": "porn", "Suggestion": "pass"}, "review"),
            ({"Label": "unexpected", "Suggestion": "pass"}, "review"),
        ]
        for item, expected in cases:
            with self.subTest(item=item):
                body = SimpleNamespace(code=200, data=SimpleNamespace(result=[item]))
                client = FakeGreenClient(SimpleNamespace(status_code=200, body=body))
                result = self.make_moderator(client).moderate_image(self.temp.name)
                self.assertFalse(result["pass"])
                self.assertEqual(result["details"][0]["suggestion"], expected)

    def test_missing_file_and_missing_dependencies_fail_closed(self):
        self.assertFalse(self.make_moderator().moderate_image(self.temp.name + ".missing")["pass"])
        with patch.object(moderator, "OSS_AVAILABLE", False):
            result = self.make_moderator().moderate_image(self.temp.name)
        self.assertFalse(result["pass"])
        self.assertEqual(result["status"], "unavailable")

    def test_api_error_and_malformed_response_fail_closed(self):
        failing = FakeGreenClient(SimpleNamespace(status_code=500, body=SimpleNamespace(code=500)))
        self.assertFalse(self.make_moderator(failing).moderate_image(self.temp.name)["pass"])
        malformed = FakeGreenClient(SimpleNamespace(status_code=200, body=SimpleNamespace(code=200, data=SimpleNamespace(result=[]))))
        self.assertFalse(self.make_moderator(malformed).moderate_image(self.temp.name)["pass"])

    def test_disabled_returns_compatible_shape_without_upload(self):
        instance = self.make_moderator(config={"CONTENT_MODERATION_ENABLED": False})
        result = instance.moderate_image(self.temp.name)
        self.assertTrue(result["pass"])
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(FakeOssBucket.uploads, [])

    @unittest.skipIf(sdk_models is None, "Alibaba SDK is not installed")
    def test_real_sdk_response_models_with_no_suggestion_field(self):
        cases = [
            ({"Label": "nonLabel", "Confidence": 100}, None, True),
            ({"Label": "nonLabel", "Confidence": 100, "RiskLevel": "none"}, "none", True),
            ({"Label": "nonLabel_lib", "RiskLevel": "none"}, "none", True),
            ({"Label": "nonLabel", "RiskLevel": "low"}, "low", False),
            ({"Label": "pornographic", "Confidence": 99, "RiskLevel": "high"}, "high", False),
            ({"Label": "nonLabel", "RiskLevel": "none"}, "high", False),
            ({"Label": "nonLabel", "RiskLevel": "medium"}, "none", False),
            ({"Label": "nonLabel", "RiskLevel": "unexpected"}, "none", False),
            ({"Label": "unexpected", "RiskLevel": "none"}, "none", False),
        ]
        for item, risk_level, passed in cases:
            with self.subTest(item=item, risk_level=risk_level):
                body = sdk_models.ImageModerationResponseBody().from_map({
                    "Code": 200, "Data": {"Result": [item], "RiskLevel": risk_level}})
                client = FakeGreenClient(sdk_models.ImageModerationResponse(status_code=200, body=body))
                result = self.make_moderator(client).moderate_image(self.temp.name)
                self.assertIs(result["pass"], passed)
                self.assertEqual(result["status"], "passed" if passed else "rejected")

    def test_bad_upload_token_does_not_upload_or_moderate(self):
        for token_response in (
            SimpleNamespace(status_code=500, body=SimpleNamespace(code=200)),
            SimpleNamespace(status_code=200, body=SimpleNamespace(code=500)),
            SimpleNamespace(status_code=200, body=SimpleNamespace(code=200, data=None)),
            SimpleNamespace(status_code=200, body=SimpleNamespace(code=200, data=SimpleNamespace())),
        ):
            with self.subTest(response=token_response):
                client = FakeGreenClient()
                client.describe_upload_token = lambda: token_response
                result = self.make_moderator(client).moderate_image(self.temp.name)
                self.assertFalse(result["pass"])
                self.assertEqual(FakeOssBucket.uploads, [])
                self.assertIsNone(client.request)

    def test_region_selects_endpoint(self):
        with patch.object(moderator, "ALIBABA_CLOUD_AVAILABLE", True), patch.object(moderator, "open_api_models", SimpleNamespace(Config=lambda **kw: kw)), patch.object(moderator, "Green20220302Client", lambda cfg: cfg):
            instance = moderator.AlibabaCloudModerator({"ALIYUN_ACCESS_KEY_ID": "id", "ALIYUN_ACCESS_KEY_SECRET": "secret", "ALIYUN_CONTENT_MODERATION_REGION": "ap-southeast-1"})
        self.assertEqual(instance.client["endpoint"], "green-cip.ap-southeast-1.aliyuncs.com")


if __name__ == "__main__":
    unittest.main()
