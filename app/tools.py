"""Tools the agent can call during a conversation."""
from google.genai import types

OUTCOMES = [
    "confirmed", "changed", "cancelled", "call_later", "wrong_number",
    "do_not_call", "no_answer", "busy", "dropped", "unclear",
]
# Set by us from the phone line, never by the agent: nobody answered, or the call ended before
# the agent recorded anything (customer hung up, line dropped).
LINE_OUTCOMES = ("no_answer", "busy", "dropped")

MARK_OUTCOME = types.FunctionDeclaration(
    name="mark_outcome",
    description="Record the result of the call once the customer has answered. Call this exactly once, before end_call.",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            # "no_answer" and "busy" are ours to set when the phone is never picked up. The agent
            # only exists once someone has answered, so it is not offered those choices.
            "outcome": types.Schema(type=types.Type.STRING, enum=[o for o in OUTCOMES if o not in LINE_OUTCOMES],
                                    description="What happened on the call."),
            "mismatch": types.Schema(type=types.Type.STRING, description="What the customer claims versus what our record shows, raised for the team to verify. Example: 'customer says already paid online by UPI; record shows COD'."),
            "new_product": types.Schema(type=types.Type.STRING, description="Product and quantity the customer says they ordered, if different from our record. A claim to verify, not a change."),
            "new_address": types.Schema(type=types.Type.STRING, description="New delivery address the customer confirmed after you read it back, with house number and 6-digit PIN code."),
            "new_delivery_date": types.Schema(type=types.Type.STRING, description="New delivery date if the customer changed it."),
            "cancel_reason": types.Schema(type=types.Type.STRING, description="Reason given if cancelled."),
            "callback_time": types.Schema(type=types.Type.STRING, description="When to call back if the customer asked for a later call."),
            "notes": types.Schema(type=types.Type.STRING, description="Anything else the team should know, in one or two sentences."),
        },
        required=["outcome"],
    ),
)

RECORD_ADDRESS = types.FunctionDeclaration(
    name="record_address",
    description=("Write down a delivery address the customer gave on this call, one part per field, exactly as they said it. "
                 "Call it as soon as you have all four parts, BEFORE reading the address back. The reply tells you whether it "
                 "was accepted and what to say next. Never fill a part the customer did not say."),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "house": types.Schema(type=types.Type.STRING, description="House, flat or plot number, as said."),
            "area": types.Schema(type=types.Type.STRING, description="Street, society, sector, colony or area."),
            "city": types.Schema(type=types.Type.STRING, description="City or town."),
            "pin": types.Schema(type=types.Type.STRING, description="PIN code, six digits."),
            "landmark": types.Schema(type=types.Type.STRING, description="Landmark, if they gave one."),
        },
        required=["house", "area", "city", "pin"],
    ),
)

END_CALL = types.FunctionDeclaration(
    name="end_call",
    description="Hang up the call. Call this only after saying goodbye and after mark_outcome.",
    parameters=types.Schema(type=types.Type.OBJECT, properties={}),
)

TOOLS = [types.Tool(function_declarations=[MARK_OUTCOME, RECORD_ADDRESS, END_CALL])]
