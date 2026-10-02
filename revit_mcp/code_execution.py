# -*- coding: UTF-8 -*-
"""
Code Execution Module for Revit MCP
Handles direct execution of IronPython code in Revit context.
"""
from pyrevit import routes, revit, DB
import json
import logging
import sys
import traceback
from StringIO import StringIO

from utils import normalize_string

# Standard logger setup
logger = logging.getLogger(__name__)


def _safe_print_str(arg):
    """Convert a print() argument to unicode without corrupting accents.

    IronPython 2's str() on a unicode value containing non-ASCII characters
    re-encodes it through the system default codec (cp1252/ascii) instead of
    UTF-8, which silently mangles accented characters (e.g. the 'é' in
    'TramCité' becomes byte 0xE9 instead of the UTF-8 sequence 0xC3 0xA9).
    That corrupted byte string then crashes pyRevit's JSON encoder later on.
    Routing every argument through normalize_string() keeps it as proper
    unicode end-to-end.
    """
    if isinstance(arg, (unicode, str)):
        return normalize_string(arg)
    return normalize_string(unicode(arg))


def register_code_execution_routes(api):
    """Register code execution routes with the API."""

    @api.route("/execute_code/", methods=["POST"])
    def execute_code(doc, uidoc, request):
        """
        Execute IronPython code in Revit context.

        Expected payload:
        {
            "code": "python code as string",
            "description": "optional description of what the code does",
            "use_transaction": true   # set false for UI ops like switching the active view
        }
        """
        try:
            # Parse the request data
            data = (
                json.loads(request.data)
                if isinstance(request.data, str)
                else request.data
            )
            code_to_execute = data.get("code", "")
            description = normalize_string(data.get("description", "Code execution"))

            if not code_to_execute:
                return routes.make_response(
                    data={"error": "No code provided"}, status=400
                )

            logger.info("Executing code: {}".format(description))

            old_stdout = sys.stdout
            captured_output = StringIO()
            sys.stdout = captured_output

            namespace = {
                "doc": doc,
                "uidoc": uidoc,
                "DB": DB,
                "revit": revit,
                "__builtins__": __builtins__,
                "print": lambda *args: captured_output.write(
                    u" ".join(_safe_print_str(arg) for arg in args) + u"\n"
                ),
            }

            try:
                exec(code_to_execute, namespace)

                sys.stdout = old_stdout
                output = normalize_string(captured_output.getvalue())
                captured_output.close()

                return routes.make_response(
                    data={
                        "status": "success",
                        "description": description,
                        "output": (
                            output
                            if output
                            else "Code executed successfully (no output)"
                        ),
                        "code_executed": normalize_string(code_to_execute),
                    }
                )

            except Exception as exec_error:
                sys.stdout = old_stdout
                partial_output = normalize_string(captured_output.getvalue())
                captured_output.close()

                error_traceback = normalize_string(traceback.format_exc())
                error_type = type(exec_error).__name__
                error_msg = normalize_string(str(exec_error))
                enhanced_message = u"{}: {}".format(error_type, error_msg)

                hints = []
                if error_type == "AttributeError":
                    if "Name" in error_msg:
                        hints.append(
                            "The 'Name' property may not be directly accessible in IronPython. "
                            "Try getattr(element, 'Name', 'N/A') or "
                            "element.get_Parameter(DB.BuiltInParameter.ALL_MODEL_TYPE_NAME).AsString()"
                        )
                    else:
                        hints.append(
                            "Some Revit API properties are not directly accessible in IronPython. "
                            "Try getattr(obj, 'property_name', default_value) for safe access."
                        )
                elif error_type == "NullReferenceException" or "NoneType" in error_msg:
                    hints.append(
                        "An object is None/null. Check if elements exist before "
                        "accessing their properties: 'if element:'"
                    )
                elif error_type == "InvalidOperationException":
                    hints.append(
                        "This operation may require a transaction. Wrap model-modifying "
                        "code in: t = DB.Transaction(doc, 'desc'); t.Start(); ...; t.Commit()"
                    )

                logger.error("Code execution failed: {}".format(enhanced_message))

                response_data = {
                    "status": "error",
                    "error": enhanced_message,
                    "error_type": error_type,
                    "traceback": error_traceback,
                    "code_attempted": normalize_string(code_to_execute),
                }

                if partial_output:
                    response_data["partial_output"] = partial_output

                if hints:
                    response_data["hints"] = hints

                return routes.make_response(data=response_data, status=500)

        except Exception as e:
            logger.error("Execute code request failed: {}".format(str(e)))
            return routes.make_response(
                data={"error": normalize_string(str(e))}, status=500
            )

    logger.info("Code execution routes registered successfully.")
