"""Tools the agent can call during a conversation."""
from google.genai import types

OUTCOMES = [
    "confirmed", "changed", "cancelled", "call_later", "wrong_number",
    "do_not_call", "no_answer", "unclear",
]

MARK_OUTCOME = types.FunctionDeclaration(
    name="mark_outcome",
    description="Record the result of the call once the customer has answered. Call this exactly once, before end_call.",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "outcome": types.Schema(type=types.Type.STRING, enum=OUTCOMES,
                                    description="What happened on the call."),
            "new_address": types.Schema(type=types.Type.STRING, description="New delivery address if the customer changed it."),
            "new_delivery_date": types.Schema(type=types.Type.STRING, description="New delivery date if the customer changed it."),
            "cancel_reason": types.Schema(type=types.Type.STRING, description="Reason given if cancelled."),
            "callback_time": types.Schema(type=types.Type.STRING, description="When to call back if the customer asked for a later call."),
            "notes": types.Schema(type=types.Type.STRING, description="Anything else the team should know, in one or two sentences."),
        },
        required=["outcome"],
    ),
)

END_CALL = types.FunctionDeclaration(
    name="end_call",
    description="Hang up the call. Call this only after saying goodbye and after mark_outcome.",
    parameters=types.Schema(type=types.Type.OBJECT, properties={}),
)

TOOLS = [types.Tool(function_declarations=[MARK_OUTCOME, END_CALL])]
