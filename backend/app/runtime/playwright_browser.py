from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from playwright.async_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    Playwright,
    async_playwright,
)

from app.config import get_settings
from app.runtime.browser import FieldContract, ObservedPage, PageContract, url_matches


class BrowserExecutionError(RuntimeError):
    pass


class BrowserSubmitUnknownOutcome(BrowserExecutionError):
    pass


class PlaywrightPageSession:
    def __init__(
        self,
        run_id: str,
        node_id: str,
        url: str,
        contract: PageContract,
        config: dict[str, Any],
    ) -> None:
        self.run_id = run_id
        self.node_id = node_id
        self.url = url
        self.contract = contract
        self.config = config
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        self.artifacts: list[dict[str, Any]] = []

    async def __aenter__(self) -> PlaywrightPageSession:
        if (
            urlparse(self.url).scheme not in {"http", "https"}
            or not url_matches(self.contract.url_pattern, self.url)
        ):
            raise BrowserExecutionError("目标 URL 不符合 PageContract，已阻止导航")
        self._playwright = await async_playwright().start()
        download_root = (
            get_settings().artifact_root / self.run_id / f"downloads-{self.node_id}"
        ).resolve()
        download_root.mkdir(parents=True, exist_ok=True)
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            downloads_path=str(download_root),
        )
        self._context = await self._browser.new_context(accept_downloads=True)
        self.page = await self._context.new_page()
        await self.page.goto(
            self.url,
            wait_until="domcontentloaded",
            timeout=int(self.config.get("navigationTimeoutMs", 15000)),
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._context is not None:
            await self._context.close()
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()

    def _page(self) -> Page:
        if self.page is None:
            raise BrowserExecutionError("Playwright 页面尚未初始化")
        return self.page

    def _locator(self, descriptor: dict[str, str]) -> Locator:
        page = self._page()
        if "css" in descriptor:
            return page.locator(descriptor["css"])
        if "label" in descriptor and "role" not in descriptor:
            return page.get_by_label(descriptor["label"], exact=True)
        if "role" in descriptor:
            name = descriptor.get("name") or descriptor.get("label")
            return page.get_by_role(descriptor["role"], name=name, exact=True)
        raise BrowserExecutionError("Locator 必须声明 css、label 或 role")

    async def inspect_and_fill(
        self,
        form_values: dict[str, Any],
    ) -> ObservedPage:
        page = self._page()
        body = await page.locator("body").inner_html()
        fingerprint = "sha256:" + hashlib.sha256(body.encode()).hexdigest()
        observed_fields: list[FieldContract] = []
        located_handles: set[str] = set()
        for field in self.contract.fields:
            locator = self._locator(field.locator)
            if await locator.count() != 1:
                continue
            observed_fields.append(await self._inspect_field(field.id, locator, field.locator))
            handle = await locator.evaluate(
                "(element) => element.id || element.getAttribute('name') || "
                "element.getAttribute('aria-label') || ''"
            )
            if handle:
                located_handles.add(str(handle))

        controls = page.locator("input, select, textarea")
        for index in range(await controls.count()):
            control = controls.nth(index)
            field_id = await control.evaluate(
                "(element) => element.getAttribute('data-contract-id') || element.id || "
                "element.getAttribute('name') || element.getAttribute('aria-label') || ''"
            )
            if not field_id or str(field_id) in located_handles:
                continue
            observed_fields.append(
                await self._inspect_field(
                    str(field_id),
                    control,
                    {"css": f'[data-contract-id="{field_id}"]'},
                )
            )

        for field in self.contract.fields:
            if field.id not in form_values:
                continue
            locator = self._locator(field.locator)
            if await locator.count() != 1:
                continue
            await self._fill(locator, form_values[field.id])

        identity = await self._optional_text(self.config.get("identitySelector"))
        business_object = await self._optional_text(
            self.config.get("businessObjectSelector")
        )
        submit = self.contract.submit
        if submit and await self._locator(submit).count() != 1:
            submit = None
        observed = ObservedPage(
            url=page.url,
            title=await page.title(),
            identity=identity,
            businessObject=business_object,
            fingerprint=fingerprint,
            fields=observed_fields,
            submit=submit,
            security_warning=not page.url.startswith(("http://", "https://")),
        )
        await self._write_json_artifact(
            "browser-dom-summary.json",
            {"observedPage": observed.model_dump(by_alias=True)},
            "browser-dom-summary",
        )
        if self._screenshots_allowed():
            await self._screenshot("browser-filled.png", "browser-filled")
        return observed

    async def submit(self) -> dict[str, Any]:
        if not self.contract.submit:
            raise BrowserExecutionError("PageContract 未声明 submit")
        page = self._page()
        clicked = False
        try:
            locator = self._locator(self.contract.submit)
            if await locator.count() != 1:
                raise BrowserExecutionError("提交动作无法唯一定位")
            clicked = True
            await locator.click(timeout=int(self.config.get("actionTimeoutMs", 10000)))
            for assertion in self.config.get("successAssertions", []):
                await self._assert(assertion)
            result: dict[str, Any] = {"status": "succeeded"}
            for key, selector in self.config.get("resultSelectors", {}).items():
                result[key] = (await page.locator(selector).inner_text()).strip()
            if self._screenshots_allowed():
                await self._screenshot("browser-submitted.png", "browser-submitted")
            return result
        except Exception as exc:
            if clicked:
                raise BrowserSubmitUnknownOutcome(
                    f"提交动作已触发，但无法确认结果: {exc}"
                ) from exc
            raise BrowserExecutionError(str(exc)) from exc

    async def _inspect_field(
        self,
        field_id: str,
        locator: Locator,
        descriptor: dict[str, str],
    ) -> FieldContract:
        details = await locator.evaluate(
            """(element) => ({
                tag: element.tagName.toLowerCase(),
                type: element.type || element.tagName.toLowerCase(),
                required: Boolean(element.required),
                options: element.tagName.toLowerCase() === 'select'
                    ? Array.from(element.options).map((item) => item.value)
                    : []
            })"""
        )
        field_type = details["type"]
        if details["tag"] == "textarea":
            field_type = "textarea"
        return FieldContract(
            id=field_id,
            required=details["required"],
            type=field_type,
            locator=descriptor,
            options=details["options"],
        )

    @staticmethod
    async def _fill(locator: Locator, value: Any) -> None:
        input_type = await locator.get_attribute("type")
        tag_name = await locator.evaluate("(element) => element.tagName.toLowerCase()")
        if input_type in {"checkbox", "radio"}:
            if bool(value):
                await locator.check()
            else:
                await locator.uncheck()
        elif tag_name == "select":
            await locator.select_option(str(value))
        else:
            await locator.fill(str(value))

    async def _optional_text(self, selector: Any) -> str | None:
        if not isinstance(selector, str) or not selector:
            return None
        locator = self._page().locator(selector)
        if await locator.count() != 1:
            return None
        return (await locator.inner_text()).strip()

    async def _assert(self, assertion: dict[str, Any]) -> None:
        page = self._page()
        timeout = int(assertion.get("timeoutMs", 10000))
        kind = assertion.get("type")
        if kind == "text":
            await page.get_by_text(assertion["value"], exact=False).wait_for(timeout=timeout)
        elif kind == "selector":
            await page.locator(assertion["value"]).wait_for(timeout=timeout)
        elif kind == "url":
            await page.wait_for_url(assertion["value"], timeout=timeout)
        else:
            raise BrowserExecutionError(f"不支持的提交断言: {kind}")

    def _screenshots_allowed(self) -> bool:
        return bool(self.config.get("captureScreenshots", True)) and not any(
            field.sensitive for field in self.contract.fields
        )

    async def _screenshot(self, name: str, kind: str) -> None:
        artifact_id, target = self._artifact_target()
        await self._page().screenshot(path=str(target), type="png", full_page=True)
        self.artifacts.append(self._artifact(artifact_id, target, name, kind, "image/png"))

    async def _write_json_artifact(
        self,
        name: str,
        payload: dict[str, Any],
        kind: str,
    ) -> None:
        artifact_id, target = self._artifact_target()
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.artifacts.append(
            self._artifact(
                artifact_id,
                target,
                name,
                kind,
                "application/json",
            )
        )

    def _artifact_target(self) -> tuple[str, Path]:
        artifact_id = str(uuid4())
        root = (get_settings().artifact_root / self.run_id).resolve()
        root.mkdir(parents=True, exist_ok=True)
        return artifact_id, root / artifact_id

    def _artifact(
        self,
        artifact_id: str,
        path: Path,
        name: str,
        kind: str,
        media_type: str,
    ) -> dict[str, Any]:
        content = path.read_bytes()
        return {
            "id": artifact_id,
            "kind": kind,
            "name": name,
            "uri": f"artifact://{artifact_id}",
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
            "mediaType": media_type,
            "storageKey": artifact_id,
            "metadata": {"nodeId": self.node_id},
        }
