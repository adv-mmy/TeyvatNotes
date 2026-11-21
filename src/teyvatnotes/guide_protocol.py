"""服务端和桌面共享的输出错误契约。只暴露固定原因，不转发上游正文。"""

GUIDE_OUTPUT_ERRORS = {
    "answer_output_limit_reached": "回答达到输出上限，未能生成完整攻略。",
    "model_incomplete": "模型未返回完整回答，未确认是输出上限所致。",
    "invalid_model_response": "模型响应格式无效，无法读取回答。",
    "invalid_answer_structure": "模型返回的攻略格式不符合要求，未展示答案。",
    "invalid_answer_citation": "攻略引用未通过本轮证据校验，未展示答案。",
    "model_refused": "模型拒绝了本次回答。",
    "model_response_too_large": "模型响应超过可接收的字节数，未展示答案。",
    "model_identity_mismatch": "返回的模型标识与服务端配置不一致。",
    "answer_equipment_not_owned": "回答依赖未确认持有的装备，未展示答案。",
    "answer_character_not_owned": "回答依赖未确认持有的角色，未展示答案。",
    "incomplete_quest_answer": "任务攻略缺少必要步骤，未展示答案。",
    "invalid_quest_order": "任务攻略的步骤顺序未通过校验，未展示答案。",
    "answer_too_large": "带引用的攻略超过服务返回大小限制，未展示答案。",
}
