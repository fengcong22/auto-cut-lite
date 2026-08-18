from typing import Dict, List, Optional, Union

from utils.formatters import safe_tim
from utils.text_template_adapter import (
    apply_text_template_texts,
    build_text_template_segment,
    remap_text_template_bundle_ids,
    resolve_text_template_bundle,
)


class TextTemplateOpsMixin:
    def add_text_template(
        self,
        template_id: Optional[str] = None,
        template_query: Optional[str] = None,
        text: Optional[str] = None,
        texts: Optional[List[str]] = None,
        start_time: Union[str, int, None] = None,
        duration: Union[str, int] = "3s",
        track_name: str = "TextTemplateTrack",
        template_payload: Optional[Dict[str, object]] = None,
        template_payload_path: Optional[str] = None,
        return_resolved_asset: bool = False,
    ):
        if text is not None and texts is not None:
            raise ValueError("add_text_template accepts either text or texts, not both")
        if text is None and texts is None:
            raise ValueError("add_text_template requires text or texts")

        resolved_asset = None
        if template_query:
            resolved_value, resolved_asset = self._resolve_asset_write_value(
                template_query,
                "text_template",
                field_name="template_query",
            )
            template_id = template_id or resolved_value

        if not template_id and template_payload is None and not template_payload_path:
            raise ValueError(
                "add_text_template requires template_id, template_query, or template_payload(_path)"
            )

        text_values = [str(text)] if text is not None else [str(item) for item in (texts or [])]
        bundle = resolve_text_template_bundle(
            template_id=template_id,
            texts=text_values,
            template_payload=template_payload,
            template_payload_path=template_payload_path,
        )
        bundle = apply_text_template_texts(bundle, text_values)
        bundle = remap_text_template_bundle_ids(bundle)

        start_us = safe_tim(
            start_time if start_time is not None else self.get_track_duration(track_name)
        )
        duration_us = safe_tim(duration)
        segment_payload = build_text_template_segment(
            bundle, start_us=start_us, duration_us=duration_us
        )
        self._pending_text_template_bundle = bundle
        self._pending_text_template_segment_payload = segment_payload

        if getattr(self, "is_patch_mode", False):
            result = self.patch_session.add_text_template_segment(
                segment_payload,
                bundle,
                track_name,
            )
            if return_resolved_asset:
                return result, resolved_asset
            return result

        pending_bundle = bundle
        pending_segment_payload = segment_payload

        def _apply_pending_text_template():
            session = self.patch_session
            result_inner = session.add_text_template_segment(
                pending_segment_payload,
                pending_bundle,
                track_name,
            )
            session.save()
            return result_inner

        self._pending_post_save_patches.append(_apply_pending_text_template)
        result = {
            "segment_id": segment_payload["id"],
            "track_name": track_name,
            "track_type": "text",
            "segment_type": "text_template",
            "start_us": start_us,
            "duration_us": duration_us,
            "material_id": bundle["text_template"]["id"],
            "resource_id": bundle["text_template"].get("resource_id"),
        }
        if return_resolved_asset:
            return result, resolved_asset
        return result
