#!/usr/bin/env python3
"""Deallocate this Azure VM through its system-assigned managed identity."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


API_VERSION = "2024-07-01"
IMDS_COMPUTE_URL = "http://169.254.169.254/metadata/instance/compute?api-version=2021-02-01"


def request_json(url: str, *, headers: dict[str, str], method: str = "GET") -> dict:
    request = urllib.request.Request(url, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read()
    return json.loads(payload) if payload else {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify identity and VM access without deallocating",
    )
    args = parser.parse_args()

    resource = urllib.parse.quote("https://management.azure.com/", safe="")
    token_url = (
        "http://169.254.169.254/metadata/identity/oauth2/token"
        f"?api-version=2018-02-01&resource={resource}"
    )
    token_payload = request_json(token_url, headers={"Metadata": "true"})
    token = token_payload.get("access_token")
    if not token:
        raise RuntimeError("Azure managed identity did not return an access token")

    # Abonelik, kaynak grubu ve VM adı koda gömülmez: ortam değişkeni verilmezse
    # VM'nin kendi Instance Metadata Service kaydından okunur.
    compute = request_json(IMDS_COMPUTE_URL, headers={"Metadata": "true"})
    subscription_id = os.environ.get("AZURE_SUBSCRIPTION_ID") or compute["subscriptionId"]
    resource_group = os.environ.get("AZURE_RESOURCE_GROUP") or compute["resourceGroupName"]
    vm_name = os.environ.get("AZURE_VM_NAME") or compute["name"]

    vm_resource_url = (
        "https://management.azure.com"
        f"/subscriptions/{subscription_id}"
        f"/resourceGroups/{resource_group}"
        f"/providers/Microsoft.Compute/virtualMachines/{vm_name}"
    )
    if args.check:
        request_json(
            f"{vm_resource_url}?api-version={API_VERSION}",
            headers={"Authorization": f"Bearer {token}"},
        )
        print("Managed identity and VM access are ready.")
        return 0

    vm_url = f"{vm_resource_url}/deallocate?api-version={API_VERSION}"
    request_json(
        vm_url,
        headers={"Authorization": f"Bearer {token}", "Content-Length": "0"},
        method="POST",
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, KeyError, urllib.error.HTTPError) as exc:
        print(f"Azure deallocation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
