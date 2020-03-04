# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
import json
import logging
from typing import Sequence
from urllib.parse import urlparse

from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.util import ns_to_iso_str
from opentelemetry.trace import Span, SpanKind
from opentelemetry.trace.status import StatusCanonicalCode

from azure_monitor import protocol, utils
from azure_monitor.exporter import BaseExporter

logger = logging.getLogger(__name__)


class AzureMonitorSpanExporter(BaseExporter, SpanExporter):
    def __init__(self, **options):
        super(AzureMonitorSpanExporter, self).__init__(**options)

    def export(self, spans: Sequence[Span]) -> SpanExportResult:
        envelopes = map(self.span_to_envelope, spans)
        envelopes_to_export = map(
            lambda x: x.to_dict(),
            tuple(self.apply_telemetry_processors(envelopes)),
        )
        try:
            result = self._transmit(envelopes_to_export)
            if result == utils.ExportResult.FAILED_RETRYABLE:
                self.storage.put(envelopes, result)
            if len(list(envelopes_to_export)) < self.options.max_batch_size:
                self._transmit_from_storage()
            return utils.get_trace_export_result(result)
        except Exception:
            logger.exception("Exception occurred while exporting the data.")

    def span_to_envelope(
        self, span: Span
    ) -> protocol.Envelope:  # noqa pylint: disable=too-many-branches

        if not span:
            return None
        # pylint: disable=too-many-statements
        envelope = protocol.Envelope(
            ikey=self.options.instrumentation_key,
            tags=dict(utils.azure_monitor_context),
            time=ns_to_iso_str(span.start_time),
        )
        envelope.tags["ai.operation.id"] = "{:032x}".format(
            span.context.trace_id
        )
        parent = span.parent
        if isinstance(parent, Span):
            parent = parent.context
        if parent:
            envelope.tags["ai.operation.parentId"] = "{:016x}".format(
                parent.span_id
            )
        if span.kind in (SpanKind.CONSUMER, SpanKind.SERVER):
            envelope.name = "Microsoft.ApplicationInsights.Request"
            data = protocol.Request(
                id="{:016x}".format(span.context.span_id),
                duration=utils.ns_to_duration(span.end_time - span.start_time),
                response_code=str(span.status.canonical_code.value),
                success=span.status.canonical_code
                == StatusCanonicalCode.OK,  # Modify based off attributes or Status
                properties={},
            )
            envelope.data = protocol.Data(
                base_data=data, base_type="RequestData"
            )
            if "http.method" in span.attributes:
                data.name = span.attributes["http.method"]
                if "http.route" in span.attributes:
                    data.name = data.name + " " + span.attributes["http.route"]
                    envelope.tags["ai.operation.name"] = data.name
                    data.properties["request.name"] = data.name
                elif "http.path" in span.attributes:
                    data.properties["request.name"] = (
                        data.name + " " + span.attributes["http.path"]
                    )
            if "http.url" in span.attributes:
                data.url = span.attributes["http.url"]
                data.properties["request.url"] = span.attributes["http.url"]
            if "http.status_code" in span.attributes:
                status_code = span.attributes["http.status_code"]
                data.response_code = str(status_code)
                data.success = 200 <= status_code < 400
        else:
            envelope.name = "Microsoft.ApplicationInsights.RemoteDependency"
            data = protocol.RemoteDependency(
                name=span.name,
                id="{:016x}".format(span.context.span_id),
                result_code=str(span.status.canonical_code.value),
                duration=utils.ns_to_duration(span.end_time - span.start_time),
                success=span.status.canonical_code
                == StatusCanonicalCode.OK,  # Modify based off attributes or Status
                properties={},
            )
            envelope.data = protocol.Data(
                base_data=data, base_type="RemoteDependencyData"
            )
            if span.kind in (SpanKind.CLIENT, SpanKind.PRODUCER):
                if (
                    "component" in span.attributes
                    and span.attributes["component"] == "http"
                ):
                    data.type = "HTTP"
                if "http.url" in span.attributes:
                    url = span.attributes["http.url"]
                    # data is the url
                    data.data = url
                    parse_url = urlparse(url)
                    # TODO: error handling, probably put scheme as well
                    # target matches authority (host:port)
                    data.target = parse_url.netloc
                    if "http.method" in span.attributes:
                        # name is METHOD/path
                        data.name = (
                            span.attributes["http.method"]
                            + "/"
                            + parse_url.path
                        )
                if "http.status_code" in span.attributes:
                    status_code = span.attributes["http.status_code"]
                    data.result_code = str(status_code)
                    data.success = 200 <= status_code < 400
            else:  # SpanKind.INTERNAL
                data.type = "InProc"
                data.success = True
        for key in span.attributes:
            # This removes redundant data from ApplicationInsights
            if key.startswith("http."):
                continue
            data.properties[key] = span.attributes[key]
        if span.links:
            links = []
            for link in span.links:
                operation_id = "{:032x}".format(link.context.trace_id)
                span_id = "{:016x}".format(link.context.span_id)
                links.append({"operation_Id": operation_id, "id": span_id})
            data.properties["_MS.links"] = json.dumps(links)
        # TODO: tracestate, tags
        return envelope
