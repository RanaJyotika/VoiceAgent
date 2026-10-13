"""Tools the agent can call during a conversation."""
from google.genai import types

from . import catalog
from .call_types import DEFAULT, LINE_OUTCOMES, CallType  # noqa: F401  (LINE_OUTCOMES: re-exported)


def mark_outcome_for(call_type: CallType) -> types.FunctionDeclaration:
    """The result tool for one call type: its own results and the details it writes down."""
    props = {
        # "no_answer" and "busy" are ours to set when the phone is never picked up. The agent
        # only exists once someone has answered, so it is not offered those choices.
        "outcome": types.Schema(type=types.Type.STRING, enum=list(call_type.outcomes),
                                description="What happened on the call."),
    }
    for name, description in call_type.details.items():
        props[name] = types.Schema(type=types.Type.STRING, description=description)
    return types.FunctionDeclaration(
        name="mark_outcome",
        description="Record the result of the call once the customer has answered. Call this exactly once, before end_call.",
        parameters=types.Schema(type=types.Type.OBJECT, properties=props, required=["outcome"]),
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

PRODUCT_INFO = types.FunctionDeclaration(
    name="product_info",
    description=("Look up a product in our catalogue when the customer asks about one that is not in PRODUCT INFO. "
                 "Returns the closest products with price, options and a short description."),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={"query": types.Schema(type=types.Type.STRING, description=(
            "The product the customer asked about, in English letters, e.g. '5 mukhi rudraksha', 'karungali mala', "
            "'tiger eye bracelet'."))},
        required=["query"],
    ),
)


def tools_for(call_type: CallType) -> list[types.Tool]:
    """The tools the agent has on a call of this type. record_address only where addresses are taken;
    product_info only when the product catalogue is set up (MONGODB_URI)."""
    declarations = [mark_outcome_for(call_type)]
    if call_type.takes_address:
        declarations.append(RECORD_ADDRESS)
    if catalog.enabled():
        declarations.append(PRODUCT_INFO)
    declarations.append(END_CALL)
    return [types.Tool(function_declarations=declarations)]


TOOLS = tools_for(DEFAULT)
