# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from collections.abc import Iterable
import contextlib
import json
from unittest import mock

from django.core.cache import cache
from django.contrib.auth.models import User, Permission
from django.conf import settings
from django.forms import ValidationError
from django.urls import reverse
from django.utils.encoding import smart_str

from markus.testing import MetricsMock
import pyquery
import pytest

from crashstats import productlib
from crashstats.api.views import (
    api_models_and_names,
    is_valid_model_class,
    MultipleStringField,
    TYPE_MAP,
)
from crashstats.crashstats.models import (
    BugAssociation,
    MissingProcessedCrash,
    NoOpMiddleware,
    SocorroMiddleware,
)
from crashstats.crashstats.tests.conftest import BaseTestViews
from crashstats.supersearch.models import (
    SuperSearch,
    SuperSearchUnredacted,
    ESSocorroMiddleware,
)
from crashstats.tokens.models import Token
from crashstats.crashstats.utils import DateTimeEncoder
from socorro.lib.libooid import create_new_ooid


class TestDedentLeft:
    def test_dedent_left(self):
        from crashstats.api.views import dedent_left

        assert dedent_left("Hello", 2) == "Hello"
        assert dedent_left("   Hello", 2) == " Hello"
        assert dedent_left("   Hello ", 2) == " Hello "

        text = """Line 1
        Line 2
        Line 3
        """.rstrip()
        # because this code right above is indented with 2 * 4 spaces
        assert dedent_left(text, 8) == "Line 1\nLine 2\nLine 3"


class TestIsValidModelClass:
    """Test that is_valid_model_class validates API models."""

    @pytest.mark.parametrize("model", (SuperSearch, SuperSearchUnredacted))
    def test_valid(self, model):
        assert is_valid_model_class(model)

    @pytest.mark.parametrize(
        "not_model",
        ("SuperSearch", int, contextlib, SocorroMiddleware, ESSocorroMiddleware),
    )
    def test_invalid(self, not_model):
        assert not is_valid_model_class(not_model)


class TestDocumentationViews(BaseTestViews):
    def test_documentation_home_page(self):
        from crashstats.api import views

        models_and_names = views.api_models_and_names()
        valid_names = [pair[1] for pair in models_and_names]

        url = reverse("api:documentation")
        response = self.client.get(url)
        assert response.status_code == 200

        doc = pyquery.PyQuery(response.content)

        for elt in doc("#mainbody .panel .title h2 a"):
            assert elt.text in valid_names


class TestViews(BaseTestViews):
    def test_invalid_url(self):
        url = reverse("api:model_wrapper", args=("BlaBLabla",))
        with MetricsMock() as metrics_mock:
            response = self.client.get(url)
        assert response.status_code == 404
        # Assert no metrics were emitted
        metrics_mock.assert_not_timing("webapp.view.pageview")

    def test_base_classes_raise_not_found(self):
        url = reverse("api:model_wrapper", args=("SocorroMiddleware",))
        response = self.client.get(url)
        assert response.status_code == 404

        url = reverse("api:model_wrapper", args=("ESSocorroMiddleware",))
        response = self.client.get(url)
        assert response.status_code == 404

    def test_option_CORS(self):
        """OPTIONS request for model_wrapper returns CORS headers"""
        url = reverse("api:model_wrapper", args=("NoOp",))
        response = self.client.options(url, HTTP_ORIGIN="http://example.com")
        assert response.status_code == 200
        assert response["Access-Control-Allow-Origin"] == "*"

    def test_cache_control(self):
        """Verifies Cache-Control header for models that cache results"""
        url = reverse("api:model_wrapper", args=("NoOp",))
        response = self.client.get(
            url, {"product": productlib.get_default_product().name}
        )
        assert response.status_code == 200
        assert response["Cache-Control"]
        assert "private" in response["Cache-Control"]
        cache_seconds = NoOpMiddleware.cache_seconds
        assert f"max-age={cache_seconds}" in response["Cache-Control"]

    def test_metrics_gathering(self):
        url = reverse("api:model_wrapper", args=("NoOp",))
        with MetricsMock() as metrics_mock:
            response = self.client.get(url, {"product": "good"})
        assert response.status_code == 200
        metrics_mock.assert_timing(
            "webapp.view.pageview",
            tags=["ajax:false", "api:true", "path:/api/noop/", "status:200"],
        )

    def test_param_exceptions(self):
        # missing required parameter
        url = reverse("api:model_wrapper", args=("NoOp",))
        response = self.client.get(url)
        assert response.status_code == 400
        assert "This field is required." in smart_str(response.content)

        response = self.client.get(url, {"product": "bad"})
        assert response.status_code == 400
        assert "Bad value for parameter(s) 'Bad product'" in smart_str(response.content)

    def test_hit_or_not_hit_ratelimit(self):
        url = reverse("api:model_wrapper", args=("NoOp",))

        response = self.client.get(url, {"product": "good"})
        assert response.status_code == 200
        with self.settings(API_RATE_LIMIT="3/m", API_RATE_LIMIT_AUTHENTICATED="6/m"):
            current_limit = 3  # see above mentioned settings override
            # Double to avoid
            # https://bugzilla.mozilla.org/show_bug.cgi?id=1148470
            for i in range(current_limit * 2):
                response = self.client.get(
                    url, {"product": "good"}, HTTP_X_REAL_IP="12.12.12.12"
                )
            assert response.status_code == 429

            # But it'll work if you use a different X-Real-IP
            # because the rate limit is based on your IP address
            response = self.client.get(
                url, {"product": "good"}, HTTP_X_REAL_IP="11.11.11.11"
            )
            assert response.status_code == 200

            user = User.objects.create(username="test")
            token = Token.objects.create(
                user=user, notes="Just for avoiding rate limit"
            )

            response = self.client.get(
                url, {"product": "good"}, HTTP_AUTH_TOKEN=token.key
            )
            assert response.status_code == 200

            for i in range(current_limit):
                response = self.client.get(url, {"product": "good"})
            assert response.status_code == 200

            # But even being logged in has a limit.
            authenticated_limit = 6  # see above mentioned settings override
            assert authenticated_limit > current_limit
            for i in range(authenticated_limit * 2):
                response = self.client.get(url, {"product": "good"})
            # Even if you're authenticated - sure the limit is higher -
            # eventually you'll run into the limit there too.
            assert response.status_code == 429


class TestProcessedCrashAPI(BaseTestViews):
    @mock.patch("crashstats.crashstats.models.ProcessedCrash.get_implementation")
    def test_api(self, mock_implementation):
        public_data = {
            "addons_checked": None,
            "address": "0x8",
            "build": "20120609030536",
            "client_crash_date": "2022-06-11T06:08:45",
            "completed_datetime": "2022-06-11T06:08:57",
            "cpu_arch": "amd64",
            "date_processed": "2022-06-11T06:08:45",
            "last_crash": 371342,
            "os_name": "Mac OS X",
            "os_version": "10.6.8 10K549",
            "process_type": "parent",
            "product": "WaterWolf",
            "reason": "EXC_BAD_ACCESS / KERN_INVALID_ADDRESS",
            "release_channel": "nightly",
            "release_channel": "nightly",
            "signature": "FakeSignature1",
            "success": True,
            "uptime": 14693,
            "uuid": "11cb72f5-eb28-41e1-a8e4-849982120611",
            "version": "5.0a1",
        }

        protected_data = {
            "url": "https://example.com",
            "user_comments": "no comment",
        }

        def mocked_get(**params):
            if "datatype" in params and params["datatype"] == "processed":
                return dict(**public_data, **protected_data)
            raise NotImplementedError

        mock_implementation.return_value.get.side_effect = mocked_get

        # If you don't specify a crash id, you get an HTTP 400
        url = reverse("api:model_wrapper", args=("ProcessedCrash",))
        response = self.client.get(url)
        assert response.status_code == 400
        assert response["Content-Type"] == "application/json"
        dump = json.loads(response.content)
        assert dump["errors"]["crash_id"]

        # If you don't have permissions, you only see public data
        response = self.client.get(url, {"crash_id": "123"})
        assert response.status_code == 200
        data = json.loads(response.content)
        for key in public_data.keys():
            assert key in data

        for key in protected_data.keys():
            assert key not in data

        # If you have permissions, you see all the data
        user = User.objects.create(username="test")
        self._add_permission(user, "view_pii")
        view_pii_perm = Permission.objects.get(codename="view_pii")
        token = Token.objects.create(user=user, notes="test token")
        token.permissions.add(view_pii_perm)

        response = self.client.get(url, {"crash_id": "123"}, HTTP_AUTH_TOKEN=token.key)
        assert response.status_code == 200
        dump = json.loads(response.content)
        for key in public_data.keys():
            assert key in dump

        for key in protected_data.keys():
            assert key in dump


class TestRawCrashAPI(BaseTestViews):
    @mock.patch("crashstats.crashstats.models.RawCrash.get_implementation")
    def test_api(self, mock_implementation):
        public_data = {
            "AdapterDeviceID": "0x  46",
            "AdapterVendorID": "0x8086",
            "Add-ons": "activities%40gaiamobile.org:0.1,%40gaiam...",
            "AsyncShutdownTimeout": "12345",
            "BuildID": "20130422105838",
            "CrashTime": "1366703112",
            "EMCheckCompatibility": "true",
            "InstallTime": "1366691881",
            "Notes": "AdapterVendorID: 0x8086, AdapterDeviceID: ...",
            "ProductID": "{ec8030f7-c20a-464f-9b0e-13a3a9e97384}",
            "ProductName": "WaterWolf",
            "ReleaseChannel": "nightly",
            "SecondsSinceLastCrash": "23484",
            "StartupTime": "1366702830",
            "submitted_timestamp": "2013-04-29T16:42:28.961187+00:00",
            "Vendor": "Mozilla",
            "Version": "23.0a1",
        }
        protected_data = {
            "Comments": "I visited http://example.com and mail@example.com",
            "URL": "http://system.gaiamobile.org:8080/",
        }

        def mocked_get(**params):
            if "uuid" in params and params["uuid"] == "abc123":
                return dict(**public_data, **protected_data)
            raise NotImplementedError

        mock_implementation.return_value.get.side_effect = mocked_get

        # No crash id yields HTTP 400
        url = reverse("api:model_wrapper", args=("RawCrash",))
        response = self.client.get(url)
        assert response.status_code == 400
        assert response["Content-Type"] == "application/json"
        dump = json.loads(response.content)
        assert dump["errors"]["crash_id"]

        # If you don't have permissions (not authenticated), then you get only public
        # data
        response = self.client.get(url, {"crash_id": "abc123"})
        assert response.status_code == 200
        dump = json.loads(response.content)
        for key in public_data.keys():
            assert key in dump

        for key in protected_data.keys():
            assert key not in dump

        # If you do have permissions, then you get it all
        user = User.objects.create(username="test")
        self._add_permission(user, "view_pii")
        view_pii_perm = Permission.objects.get(codename="view_pii")
        token = Token.objects.create(user=user, notes="test token")
        token.permissions.add(view_pii_perm)

        response = self.client.get(
            url, {"crash_id": "abc123"}, HTTP_AUTH_TOKEN=token.key
        )
        assert response.status_code == 200
        dump = json.loads(response.content)
        for key in public_data.keys():
            assert key in dump

        for key in protected_data.keys():
            assert key in dump

    @mock.patch("crashstats.crashstats.models.RawCrash.get_implementation")
    def test_binary_blob(self, mock_implementation):
        def mocked_get(**params):
            if "uuid" in params and params["uuid"] == "abc":
                return "\xe0"
            raise NotImplementedError

        mock_implementation.return_value.get.side_effect = mocked_get

        url = reverse("api:model_wrapper", args=("RawCrash",))
        response = self.client.get(url, {"crash_id": "abc", "format": "raw"})
        # because we don't have permission
        assert response.status_code == 403

        response = self.client.get(url, {"crash_id": "abc", "format": "wrong"})  # note
        # invalid format
        assert response.status_code == 400
        assert response["Content-Type"] == "application/json"

        user = self._login()
        self._add_permission(user, "view_pii")
        response = self.client.get(url, {"crash_id": "abc", "format": "raw"})
        # still don't have the right permission
        assert response.status_code == 403

        self._add_permission(user, "view_rawdump")
        response = self.client.get(url, {"crash_id": "abc", "format": "raw"})
        # finally!
        assert response.status_code == 200
        assert response["Content-Disposition"] == 'attachment; filename="abc.dmp"'
        assert response["Content-Type"] == "application/octet-stream"

    def test_invalid_crash_id(self):
        url = reverse("api:model_wrapper", args=("RawCrash",))
        response = self.client.get(
            url, {"crash_id": "821fcd0c-d925-4900-85b6-687250180607docker/as_me.sh"}
        )
        assert response.status_code == 400


class TestBugs(BaseTestViews):
    def test_api(self):
        BugAssociation.objects.create(bug_id="999999", signature="OOM | small")
        url = reverse("api:model_wrapper", args=("Bugs",))
        response = self.client.get(url)
        assert response.status_code == 400
        assert response["Content-Type"] == "application/json"
        dump = json.loads(response.content)
        assert dump["errors"]["signatures"]

        response = self.client.get(url, {"signatures": "OOM | small"})
        assert response.status_code == 200
        assert json.loads(response.content) == {
            "hits": [{"id": 999999, "signature": "OOM | small"}],
            "total": 1,
        }


class TestSignaturesForBugs(BaseTestViews):
    def test_api(self):
        BugAssociation.objects.create(bug_id="999999", signature="OOM | small")

        url = reverse("api:model_wrapper", args=("SignaturesByBugs",))
        response = self.client.get(url)
        assert response.status_code == 400
        assert response["Content-Type"] == "application/json"
        dump = json.loads(response.content)
        assert dump["errors"]["bug_ids"]

        response = self.client.get(url, {"bug_ids": "999999"})
        assert response.status_code == 200
        assert json.loads(response.content) == {
            "hits": [{"id": 999999, "signature": "OOM | small"}],
            "total": 1,
        }


class TestField(BaseTestViews):
    def test_api(self):
        url = reverse("api:model_wrapper", args=("Field",))
        response = self.client.get(url)
        assert response.status_code == 404


class TestSuperSearch(BaseTestViews):
    @mock.patch("crashstats.supersearch.models.SuperSearch.get_implementation")
    def test_api(self, mock_implementation):
        def mocked_get(**params):
            restricted_params = ("_facets", "_aggs.signature", "_histogram.date")
            for key in restricted_params:
                if key in params:
                    assert "url" not in params[key]

            if "product" in params:
                assert params["product"] == ["WaterWolf", "NightTrain"]

            return {
                "hits": [
                    {
                        "signature": "abcdef",
                        "product": "WaterWolf",
                        "version": "1.0",
                        "url": "http://embarassing.website.com",
                        "user_comments": "hey I am thebig@lebowski.net",
                    }
                ],
                "facets": {"signature": []},
                "total": 0,
            }

        mock_implementation.return_value.get.side_effect = mocked_get

        url = reverse("api:model_wrapper", args=("SuperSearch",))
        response = self.client.get(url)
        assert response.status_code == 200
        res = json.loads(response.content)

        assert res["hits"]
        assert res["facets"]

        # Verify forbidden fields are not exposed.
        assert "url" not in res["hits"]

        # Verify it's not possible to use restricted parameters.
        response = self.client.get(
            url,
            {
                "url": "example.com",
                "_facets": ["url", "product"],
                "_aggs.signature": ["url", "product"],
                "_histogram.date": ["url", "product"],
            },
        )
        assert response.status_code == 200

        # Verify values can be lists.
        response = self.client.get(url, {"product": ["WaterWolf", "NightTrain"]})
        assert response.status_code == 200


class TestSuperSearchUnredacted(BaseTestViews):
    @mock.patch(
        "crashstats.supersearch.models.SuperSearchUnredacted.get_implementation"
    )
    def test_api(self, mock_implementation):
        def mocked_get(**params):
            assert "url" in params
            if "product" in params:
                assert params["product"] == ["WaterWolf", "NightTrain"]
            return {
                "hits": [
                    {
                        "signature": "abcdef",
                        "product": "WaterWolf",
                        "version": "1.0",
                        "url": "example.com",
                        "user_comments": "hey I am thebig@lebowski.net",
                    }
                ],
                "facets": {"signature": []},
                "total": 0,
            }

        mock_implementation.return_value.get.side_effect = mocked_get

        url = reverse("api:model_wrapper", args=("SuperSearchUnredacted",))
        response = self.client.get(url, {"url": "example.com"})
        assert response.status_code == 403
        assert response["Content-Type"] == "application/json"
        error = json.loads(response.content)["error"]
        permission = Permission.objects.get(codename="view_pii")
        assert permission.name in error

        # Log in to get permissions.
        user = self._login()
        self._add_permission(user, "view_pii")

        response = self.client.get(url, {"url": "example.com"})
        assert response.status_code == 200
        res = json.loads(response.content)

        assert res["hits"]
        assert res["facets"]

        # Verify forbidden fields are exposed.
        assert "url" in res["hits"][0]
        assert "thebig@lebowski.net" in res["hits"][0]["user_comments"]

        # Verify values can be lists.
        response = self.client.get(
            url, {"url": "example.com", "product": ["WaterWolf", "NightTrain"]}
        )
        assert response.status_code == 200


class TestReprocessing(BaseTestViews):
    @mock.patch("crashstats.crashstats.models.Reprocessing.get_implementation")
    def test_api(self, mock_implementation):
        crash_id = create_new_ooid()

        def mocked_publish(queue, crash_ids):
            assert queue == "reprocessing"
            assert crash_ids == [crash_id]
            return True

        mock_implementation.return_value.publish.side_effect = mocked_publish

        url = reverse("api:model_wrapper", args=("Reprocessing",))
        response = self.client.get(url)
        assert response.status_code == 403

        params = {"crash_ids": crash_id}
        response = self.client.get(url, params, HTTP_AUTH_TOKEN="somecrap")
        assert response.status_code == 403

        user = User.objects.create(username="test")
        self._add_permission(user, "reprocess_crashes")

        perm = Permission.objects.get(codename="reprocess_crashes")
        # but make a token that only has the 'reprocess_crashes'
        # permission associated with it
        token = Token.objects.create(user=user, notes="Only reprocessing")
        token.permissions.add(perm)

        response = self.client.get(url, params, HTTP_AUTH_TOKEN=token.key)
        assert response.status_code == 405

        response = self.client.post(url, params, HTTP_AUTH_TOKEN=token.key)
        assert response.status_code == 200
        assert json.loads(response.content) is True


class TestCrashVerify:
    def setup_method(self):
        cache.clear()

    @contextlib.contextmanager
    def supersearch_returns_crashes(self, uuids):
        """Mock supersearch implementation to return result with specified crashes"""

        def mocked_supersearch_get(**params):
            assert sorted(params.keys()) == [
                "_columns",
                "_fields",
                "_results_number",
                "uuid",
            ]

            return {
                "hits": [{"uuid": uuid} for uuid in uuids],
                "facets": {"signature": []},
                "total": len(uuids),
            }

        with mock.patch(
            "crashstats.supersearch.models.SuperSearch.implementation"
        ) as mock_ss:
            mock_ss.return_value.get.side_effect = mocked_supersearch_get
            yield

    def create_s3_buckets(self, s3_helper):
        bucket = settings.SOCORRO_CONFIG["resource"]["boto"]["bucket_name"]
        s3_helper.create_bucket(bucket)
        telemetry_bucket = settings.SOCORRO_CONFIG["telemetrydata"]["bucket_name"]
        s3_helper.create_bucket(telemetry_bucket)

    def test_bad_uuid(self, client):
        url = reverse("api:crash_verify")

        resp = client.get(url, {"crash_id": "foo"})
        assert resp.status_code == 400
        data = json.loads(resp.content)
        assert data == {"error": "unknown crash id"}

    def test_elastcsearch_has_crash(self, s3_helper, client):
        self.create_s3_buckets(s3_helper)

        uuid = create_new_ooid()

        with self.supersearch_returns_crashes([uuid]):
            url = reverse("api:crash_verify")
            resp = client.get(url, {"crash_id": uuid})

        assert resp.status_code == 200
        data = json.loads(resp.content)

        assert data == {
            "uuid": uuid,
            "elasticsearch_crash": True,
            "s3_raw_crash": False,
            "s3_processed_crash": False,
            "s3_telemetry_crash": False,
        }

    def test_raw_crash_has_crash(self, s3_helper, client):
        self.create_s3_buckets(s3_helper)

        uuid = create_new_ooid()
        crash_data = {"submitted_timestamp": "2018-03-14-09T22:21:18.646733+00:00"}

        bucket = settings.SOCORRO_CONFIG["resource"]["boto"]["bucket_name"]
        raw_crash_key = "v1/raw_crash/20%s/%s" % (uuid[-6:], uuid)
        s3_helper.upload_fileobj(
            bucket_name=bucket,
            key=raw_crash_key,
            data=json.dumps(crash_data).encode("utf-8"),
        )

        with self.supersearch_returns_crashes([]):
            url = reverse("api:crash_verify")
            resp = client.get(url, {"crash_id": uuid})

        assert resp.status_code == 200
        data = json.loads(resp.content)

        assert data == {
            "uuid": uuid,
            "s3_raw_crash": True,
            "s3_processed_crash": False,
            "elasticsearch_crash": False,
            "s3_telemetry_crash": False,
        }

    def test_processed_has_crash(self, s3_helper, client):
        self.create_s3_buckets(s3_helper)

        uuid = create_new_ooid()
        crash_data = {
            "signature": "[@signature]",
            "uuid": uuid,
            "completed_datetime": "2022-03-14 10:56:50.902884",
        }

        bucket = settings.SOCORRO_CONFIG["resource"]["boto"]["bucket_name"]
        s3_helper.upload_fileobj(
            bucket_name=bucket,
            key="v1/processed_crash/%s" % uuid,
            data=json.dumps(crash_data, cls=DateTimeEncoder).encode("utf-8"),
        )

        with self.supersearch_returns_crashes([]):
            url = reverse("api:crash_verify")
            resp = client.get(url, {"crash_id": uuid})

        assert resp.status_code == 200
        data = json.loads(resp.content)

        assert data == {
            "uuid": uuid,
            "s3_processed_crash": True,
            "s3_raw_crash": False,
            "elasticsearch_crash": False,
            "s3_telemetry_crash": False,
        }

    def test_telemetry_has_crash(self, s3_helper, client):
        self.create_s3_buckets(s3_helper)

        uuid = create_new_ooid()
        crash_data = {
            "platform": "Linux",
            "signature": "now_this_is_a_signature",
            "uuid": uuid,
        }

        telemetry_bucket = settings.SOCORRO_CONFIG["telemetrydata"]["bucket_name"]
        s3_helper.upload_fileobj(
            bucket_name=telemetry_bucket,
            key="v1/crash_report/20%s/%s" % (uuid[-6:], uuid),
            data=json.dumps(crash_data).encode("utf-8"),
        )

        with self.supersearch_returns_crashes([]):
            url = reverse("api:crash_verify")
            resp = client.get(url, {"crash_id": uuid})

        assert resp.status_code == 200
        data = json.loads(resp.content)

        assert data == {
            "uuid": uuid,
            "s3_telemetry_crash": True,
            "s3_raw_crash": False,
            "s3_processed_crash": False,
            "elasticsearch_crash": False,
        }


class TestMissingProcessedCrash:
    def test_empty(self, client, db):
        url = reverse("api:missing_processed_crash")
        resp = client.get(url)
        assert resp.status_code == 200
        data = json.loads(resp.content)
        assert data == {"count": 0, "next": None, "previous": None, "results": []}

    def test_with_items(self, client, db):
        crash_ids = [create_new_ooid() for i in range(1005)]
        for crash_id in crash_ids:
            MissingProcessedCrash.objects.create(crash_id=crash_id)

        # Fetch and verify first page of results
        url = reverse("api:missing_processed_crash")
        resp = client.get(url)
        assert resp.status_code == 200
        data = json.loads(resp.content)
        assert data == {
            "count": 1005,
            "next": "http://testserver/api/MissingProcessedCrash/?page=2",
            "previous": None,
            "results": [
                {
                    "crash_id": crash_id,
                    "created": mock.ANY,
                    "is_processed": False,
                }
                for crash_id in crash_ids[:1000]
            ],
        }

        # Fetch and verify second page of results
        url = reverse("api:missing_processed_crash")
        resp = client.get(url, {"page": "2"})
        assert resp.status_code == 200
        data = json.loads(resp.content)
        assert data == {
            "count": 1005,
            "next": None,
            "previous": "http://testserver/api/MissingProcessedCrash/?page=1",
            "results": [
                {
                    "crash_id": crash_id,
                    "created": mock.ANY,
                    "is_processed": False,
                }
                for crash_id in crash_ids[1000:]
            ],
        }


class TestCrashSignature:
    # NOTE(willkg): This doesn't test signature generation--just the API wrapper.
    def test_no_payload(self, client):
        url = reverse("api:crash_signature")
        resp = client.post(url, content_type="application/json")
        assert resp.status_code == 400

    def test_wrong_contenttype(self, client):
        url = reverse("api:crash_signature")
        resp = client.post(url, content_type="application/multipart-formdata")
        assert resp.status_code == 415

    def test_basic(self, client):
        payload = {
            "jobs": [
                {
                    "os": "Linux",
                    "crashing_thread": 0,
                    "threads": [
                        {
                            "frames": [
                                {
                                    "frame": 0,
                                    "function": "SomeFunc",
                                    "line": 20,
                                    "file": "somefile.cpp",
                                    "module": "foo.so.5.15.0",
                                    "module_offset": "0x37a92",
                                    "offset": "0x7fc641052a92",
                                },
                                {
                                    "frame": 1,
                                    "function": "SomeOtherFunc",
                                    "line": 444,
                                    "file": "someotherfile.cpp",
                                    "module": "bar.so",
                                    "module_offset": "0x39a55",
                                    "offset": "0x7fc641044a55",
                                },
                            ]
                        }
                    ],
                }
            ]
        }

        url = reverse("api:crash_signature")
        resp = client.post(url, data=payload, content_type="application/json")
        expected_payload = {
            "results": [
                {
                    "extra": {
                        "normalized_frames": ["SomeFunc", "SomeOtherFunc"],
                        "proto_signature": "SomeFunc | SomeOtherFunc",
                    },
                    "notes": [],
                    "signature": "SomeFunc",
                }
            ]
        }

        assert resp.json() == expected_payload
        assert resp.status_code == 200


class TestMultipleStringField:
    """Test the MultipleStringField class."""

    def test_empty_list_required(self):
        """If a field is required, an empty list is a validation error."""
        field = MultipleStringField()
        with pytest.raises(ValidationError):
            field.clean([])

    def test_empty_list_optional(self):
        """If a field is optional, an empty list is valid."""
        assert MultipleStringField(required=False).clean([]) == []

    def test_good_argument(self):
        """A list with one string arguments is valid."""
        assert MultipleStringField().clean(["one"]) == ["one"]

    def test_null_arg(self):
        """A embedded null character is a validation error."""
        field = MultipleStringField()
        value = "Embeded_Null_\x00"
        with pytest.raises(ValidationError):
            field.clean([value])


API_MODEL_NAMES = [
    "Bugs",
    "CrashSignature",
    "MissingProcessedCrash",
    "NoOp",
    "ProcessedCrash",
    "RawCrash",
    "Reprocessing",
    "SignatureFirstDate",
    "SignaturesByBugs",
    "SuperSearch",
    "SuperSearchFields",
    "SuperSearchUnredacted",
    "VersionString",
]


def test_api_model_names():
    """
    Verify the expected publicly exposed API model list.

    This allows parametrized testing of the API Models, for better failure messages.
    """
    names = [name for model, name in api_models_and_names()]
    assert names == API_MODEL_NAMES


@pytest.mark.parametrize("name", API_MODEL_NAMES)
class TestAPIModels:
    MODEL = {}

    def setup_class(cls):
        """Generate the dictionary of model names to model classes."""
        for model, model_name in api_models_and_names():
            cls.MODEL[model_name] = model

    def test_api_required_permissions(self, name):
        """API_REQUIRED_PERMISSIONS is None or an iterable."""
        model_obj = self.MODEL[name]()
        req_perms = model_obj.API_REQUIRED_PERMISSIONS
        assert req_perms is None or (
            isinstance(req_perms, Iterable) and not isinstance(req_perms, str)
        )

    def test_api_binary_permissions(self, name):
        """API_BINARY_PERMISSIONS is None or an iterable."""
        model_obj = self.MODEL[name]()
        bin_perms = model_obj.API_BINARY_PERMISSIONS
        assert bin_perms is None or (
            isinstance(bin_perms, Iterable) and not isinstance(bin_perms, str)
        )

    def test_api_allowlist(self, name):
        """API_ALLOWLIST is defined."""
        model = self.MODEL[name]
        api_allowlist = model.API_ALLOWLIST
        assert (
            api_allowlist is None
            or isinstance(api_allowlist, Iterable)
            or (callable(api_allowlist) and isinstance(api_allowlist(), Iterable))
        )

    def test_get_annotated_params(self, name):
        """get_annotated_params returns a list suitable for creating the form."""
        model_obj = self.MODEL[name]()
        params = model_obj.get_annotated_params()
        for param in params:
            assert "required" in param
            assert "name" in param
            assert param["type"] in TYPE_MAP
