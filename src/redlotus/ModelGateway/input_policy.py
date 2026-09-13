"""Gateway-owned input limits; model selection and sampling settings are untouched."""

from dataclasses import dataclass
import json


class InputLimitError(ValueError):
    """The prepared input cannot fit the selected gateway's declared limits."""


@dataclass(frozen=True)
class ModelInputPolicy:
    max_files: int
    max_file_bytes: int
    max_request_bytes: int | None = None

    @classmethod
    def for_role(cls, role: str = "coordinator") -> "ModelInputPolicy":
        from redlotus.ModelGateway.model_factory import ModelTarget

        return cls.from_limits(ModelTarget.for_role(role).limits)

    @classmethod
    def from_limits(cls, values: dict) -> "ModelInputPolicy":
        return cls(
            max_files=int(values["max_files"]),
            max_file_bytes=int(values["max_file_bytes"]),
            max_request_bytes=values.get("max_request_bytes"),
        )

    def check(self, sizes: list[int]) -> None:
        if len(sizes) > self.max_files:
            raise InputLimitError(
                f"最多引用 {self.max_files} 个文件，本次为 {len(sizes)} 个。"
            )
        if any(size > self.max_file_bytes for size in sizes):
            raise InputLimitError(
                f"单个引用文件超过网关限额 {self.max_file_bytes:,} 字节。"
            )
        if self.max_request_bytes is not None and sum(sizes) > self.max_request_bytes:
            raise InputLimitError(
                f"引用文件总量超过网关请求限额 {self.max_request_bytes:,} 字节。"
            )

    def check_request_bytes(self, size: int) -> None:
        if self.max_request_bytes is not None and size > self.max_request_bytes:
            raise InputLimitError(
                f"编码后的请求为 {size:,} 字节，超过网关限额 {self.max_request_bytes:,} 字节。"
            )

    async def check_http_request(self, request) -> None:
        self.check_request_bytes(len(request.content))

    def check_messages(self, messages) -> None:
        from pydantic_ai import BinaryContent, TextContent

        encoded = 0
        for message in messages:
            for part in message.parts:
                content = getattr(part, "content", getattr(part, "args", ""))
                items = content if isinstance(content, (list, tuple)) else [content]
                for item in items:
                    if isinstance(item, BinaryContent):
                        self.check([len(item.data)])
                        encoded += 4 * ((len(item.data) + 2) // 3)
                    else:
                        text = item.content if isinstance(item, TextContent) else item
                        encoded += len(
                            (
                                text
                                if isinstance(text, str)
                                else json.dumps(text, ensure_ascii=False, default=str)
                            ).encode("utf-8")
                        )
        instructions = next(
            (
                m.instructions
                for m in reversed(messages)
                if getattr(m, "instructions", None)
            ),
            "",
        )
        self.check_request_bytes(encoded + len(instructions.encode("utf-8")))
