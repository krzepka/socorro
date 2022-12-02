# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import datetime
from functools import cache
from pathlib import Path

import docutils.core

from django.conf import settings
from django import http
from django.shortcuts import render

from crashstats import productlib
from crashstats.crashstats.decorators import pass_default_context, track_view
from crashstats.supersearch.models import SuperSearch, SuperSearchFields
from socorro.lib.libdockerflow import get_version_info, get_release_name
from socorro.lib.libmarkdown import get_markdown
from socorro.lib.libsocorrodataschema import get_schema


OPERATORS_BASE = [""]
OPERATORS_STRING = ["=", "~", "$", "^"]
OPERATORS_RANGE = [">=", "<=", "<", ">"]
OPERATORS_BOOLEAN = ["__true__"]
OPERATORS_FLAG = ["__null__"]
OPERATORS_MAP = {
    "string": OPERATORS_BASE + OPERATORS_STRING + OPERATORS_FLAG,
    "number": OPERATORS_BASE + OPERATORS_RANGE,
    "date": OPERATORS_RANGE,
    "bool": OPERATORS_BOOLEAN,
    "flag": OPERATORS_FLAG,
    "enum": OPERATORS_BASE,
}


@track_view
@pass_default_context
def home(request, default_context=None):
    context = default_context or {}

    return render(request, "docs/home.html", context)


@cache
def read_whatsnew():
    """Reads the WHATSNEW.rst file, parses it, and returns the HTML

    :returns: HTML document as string

    """
    path = Path(settings.SOCORRO_ROOT) / "WHATSNEW.rst"

    with open(path, "r") as fp:
        data = fp.read()
        parts = docutils.core.publish_parts(data, writer_name="html")

    return parts["html_body"]


@track_view
@pass_default_context
def whatsnew(request, default_context=None):
    version_info = get_version_info(settings.SOCORRO_ROOT)
    release = get_release_name(settings.SOCORRO_ROOT)
    version = version_info.get("version", "")
    commit = version_info.get("commit", "")
    if version:
        # This will show in prod
        release_url = (
            f"https://github.com/mozilla-services/socorro/releases/tag/{version}"
        )
    elif commit:
        # This will show on stage
        release_url = f"https://github.com/mozilla-services/socorro/commit/{commit}"
    else:
        release_url = ""

    context = default_context or {}
    context["whatsnew"] = read_whatsnew()
    context["release"] = release
    context["release_url"] = release_url

    return render(request, "docs/whatsnew.html", context)


@track_view
@pass_default_context
def protected_data_access(request, default_context=None):
    context = default_context or {}
    return render(request, "docs/protected_data_access.html", context)


@cache
def get_annotation_schema_data():
    annotation_schema = get_schema("raw_crash.schema.yaml")

    annotation_fields = {
        key: schema_item for key, schema_item in annotation_schema["properties"].items()
    }
    return annotation_fields


@cache
def get_processed_schema_data():
    processed_schema = get_schema("processed_crash.schema.yaml")

    processed_fields = {
        key: schema_item for key, schema_item in processed_schema["properties"].items()
    }
    return processed_fields


@track_view
@pass_default_context
def datadictionary_index(request, default_context=None):
    context = default_context or {}

    context["annotation_fields"] = get_annotation_schema_data()
    context["processed_fields"] = get_processed_schema_data()

    return render(request, "docs/datadictionary/index.html", context)


@cache
def get_processed_field_for_annotation(field):
    field_data = get_processed_schema_data()
    for key, value in field_data.items():
        if value.get("source_annotation") == field:
            return key
    return ""


def get_products_for_annotation(field):
    resp = SuperSearch().get(
        crash_report_keys=f"~{field}",
        _results_number=0,
        _facets=["product"],
        _facets_size=5,
    )
    if "product" in resp["facets"]:
        return [item["term"] for item in resp["facets"]["product"]]


def get_indexed_example_data(field):
    resp = SuperSearch().get(
        _results_number=0,
        _facets=[field],
        _facets_size=5,
    )
    if field in resp["facets"]:
        return [item["term"] for item in resp["facets"][field]]


@track_view
@pass_default_context
def datadictionary_field_doc(request, dataset, field, default_context=None):
    context = default_context or {}

    if dataset == "annotation":
        field_data = get_annotation_schema_data()
    elif dataset == "processed":
        field_data = get_processed_schema_data()
    else:
        return http.HttpResponseNotFound("Dataset not found")

    if field not in field_data:
        return http.HttpResponseNotFound("Field not found")

    field_item = field_data[field]

    # Get description and examples and render it as markdown
    description = field_item.get("description") or "no description"
    examples = field_item.get("examples") or []
    if examples:
        description = description + "\n- " + "\n- ".join(examples)

    description = get_markdown().render(description)

    search_field = ""
    search_field_query_type = ""
    example_data = []
    if dataset == "processed":
        super_search_field = SuperSearchFields().get_by_source_key(
            f"processed_crash.{field}"
        )
        if super_search_field:
            search_field = super_search_field["name"]
            search_field_query_type = super_search_field["query_type"]
            # FIXME(willkg): we're only doing this for public fields, but we could do
            # this for whatever fields the user can see
            if field_item["permissions"] == ["public"]:
                example_data = get_indexed_example_data(field)

    products = []
    processed_field = ""
    if dataset == "annotation":
        products = get_products_for_annotation(field)
        processed_field = get_processed_field_for_annotation(field)

    context.update(
        {
            "dataset": dataset,
            "field_name": field,
            "description": description,
            "data_reviews": field_item.get("data_reviews") or [],
            "example_data": example_data,
            "products_for_field": products,
            "search_field": search_field,
            "search_field_query_type": search_field_query_type,
            "source_annotation": field_item.get("source_annotation") or "",
            "processed_field": processed_field,
            "type": field_item["type"],
            "permissions": field_item["permissions"],
        }
    )

    return render(request, "docs/datadictionary/field_doc.html", context)


def get_valid_version(active_versions, product_name):
    """Return version data.

    If this is a local dev environment, then there's no version data.  However, the data
    structures involved are complex and there are a myriad of variations.

    This returns a valid version.

    :arg active_versions: map of product_name -> list of version dicts
    :arg product_name: a product name

    :returns: version as a string

    """
    default_version = {"product": product_name, "version": "80.0"}
    active_versions = active_versions.get("active_versions", {})
    versions = active_versions.get(product_name, []) or [default_version]
    return versions[0]["version"]


@track_view
@pass_default_context
def supersearch_home(request, default_context=None):
    context = default_context or {}

    product_name = productlib.get_default_product().name
    context["product_name"] = product_name
    context["version"] = get_valid_version(context["active_versions"], product_name)

    return render(request, "docs/supersearch/home.html", context)


@track_view
@pass_default_context
def supersearch_examples(request, default_context=None):
    context = default_context or {}

    product_name = productlib.get_default_product().name
    context["product_name"] = product_name
    context["version"] = get_valid_version(context["active_versions"], product_name)
    context["today"] = datetime.datetime.utcnow().date()
    context["yesterday"] = context["today"] - datetime.timedelta(days=1)
    context["three_days_ago"] = context["today"] - datetime.timedelta(days=3)

    return render(request, "docs/supersearch/examples.html", context)


@track_view
@pass_default_context
def supersearch_api(request, default_context=None):
    context = default_context or {}

    all_fields = SuperSearchFields().get().values()
    all_fields = [x for x in all_fields if x["is_returned"]]
    all_fields = sorted(all_fields, key=lambda x: x["name"].lower())

    aggs_fields = list(all_fields)

    # Those fields are hard-coded in `supersearch/models.py`.
    aggs_fields.append({"name": "product.version", "is_exposed": False})
    aggs_fields.append(
        {
            "name": "android_cpu_abi.android_manufacturer.android_model",
            "is_exposed": False,
        }
    )

    date_number_fields = [
        x for x in all_fields if x["query_type"] in ("number", "date")
    ]

    context["all_fields"] = all_fields
    context["aggs_fields"] = aggs_fields
    context["date_number_fields"] = date_number_fields

    context["operators"] = OPERATORS_MAP

    return render(request, "docs/supersearch/api.html", context)


@track_view
@pass_default_context
def signup(request, default_context=None):
    context = default_context or {}
    return render(request, "docs/signup.html", context)
