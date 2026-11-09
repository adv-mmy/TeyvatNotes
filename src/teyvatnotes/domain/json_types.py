"""领域边界使用的递归 JSON 类型。"""

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
