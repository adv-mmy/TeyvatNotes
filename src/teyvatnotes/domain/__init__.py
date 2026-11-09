"""攻略领域共享类型。"""

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
