import logging

from typing import Optional, Type
from dataclasses import dataclass

from pydantic import ValidationError, BaseModel
from flask import request, abort, make_response, jsonify, Request as FlaskRequest

from . import Request
from .page import PAGES


@dataclass
class Context:
    query: Optional[Type[BaseModel]]
    body: Optional[Type[BaseModel]]
    headers: Optional[Type[BaseModel]]
    cookies: Optional[Type[BaseModel]]


class FlaskBackend:
    def __init__(self, spectree):
        self.spectree = spectree
        self.config = spectree.config
        self.logger = logging.getLogger(__name__)

    def find_routes(self):
        for rule in self.app.url_map.iter_rules():
            if any(
                str(rule).startswith(path)
                for path in (f"/{self.config.PATH}", "/static")
            ):
                continue
            yield rule

    def bypass(self, func, method):
        if method in ["HEAD", "OPTIONS"]:
            return True
        return False

    def parse_func(self, route):
        func = self.app.view_functions[route.endpoint]
        for method in route.methods:
            yield method, func

    def parse_path(self, route):
        from werkzeug.routing import parse_rule, parse_converter_args

        subs = []
        parameters = []

        for converter, arguments, variable in parse_rule(str(route)):
            if converter is None:
                subs.append(variable)
                continue
            subs.append(f"{{{variable}}}")

            args, kwargs = [], {}

            if arguments:
                args, kwargs = parse_converter_args(arguments)

            schema = None
            if converter == "any":
                schema = {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": args,
                    },
                }
            elif converter == "int":
                schema = {
                    "type": "integer",
                    "format": "int32",
                }
                if "max" in kwargs:
                    schema["maximum"] = kwargs["max"]
                if "min" in kwargs:
                    schema["minimum"] = kwargs["min"]
            elif converter == "float":
                schema = {
                    "type": "number",
                    "format": "float",
                }
            elif converter == "uuid":
                schema = {
                    "type": "string",
                    "format": "uuid",
                }
            elif converter == "path":
                schema = {
                    "type": "string",
                    "format": "path",
                }
            elif converter == "string":
                schema = {
                    "type": "string",
                }
                for prop in ["length", "maxLength", "minLength"]:
                    if prop in kwargs:
                        schema[prop] = kwargs[prop]
            elif converter == "default":
                schema = {"type": "string"}

            parameters.append(
                {
                    "name": variable,
                    "in": "path",
                    "required": True,
                    "schema": schema,
                }
            )

        return "".join(subs), parameters

    def request_validation(
        self,
        request: FlaskRequest,
        query: Optional[Type[BaseModel]],
        body: Optional[Request],
        headers: Optional[Type[BaseModel]],
        cookies: Optional[Type[BaseModel]],
    ):
        req_query = request.args or {}
        if request.content_type == "application/json":
            parsed_body = request.get_json() or {}
        else:
            parsed_body = request.get_data() or {}
        req_headers = request.headers or {}
        req_cookies = request.cookies or {}
        request.context = Context(
            query=query.parse_obj(req_query) if query else None,
            body=body.model.parse_obj(parsed_body) if body and body.model else None,
            headers=headers.parse_obj(req_headers) if headers else None,
            cookies=cookies.parse_obj(req_cookies) if cookies else None,
        )

    def validate(
        self, func, query, body, headers, cookies, resp, before, after, *args, **kwargs
    ):
        response, req_validation_error, resp_validation_error = None, None, None
        try:
            self.request_validation(request, query, body, headers, cookies)
        except ValidationError as err:
            req_validation_error = err
            response = make_response(
                jsonify(err.errors()), self.config.VALIDATION_ERROR_CODE
            )

        before(request, response, req_validation_error, None)
        if req_validation_error:
            abort(response)

        response = make_response(func(*args, **kwargs))

        if resp and resp.has_model() and resp.validate:
            model = resp.find_model(response.status_code)
            if model:
                try:
                    model.validate(response.get_json())
                except ValidationError as err:
                    resp_validation_error = err
                    response = make_response(
                        jsonify({"message": "response validation error"}), 500
                    )

        after(request, response, resp_validation_error, None)

        return response

    def register_route(self, app):
        self.app = app
        from flask import jsonify

        self.app.add_url_rule(
            self.config.spec_url,
            "openapi",
            lambda: jsonify(self.spectree.spec),
        )

        for ui in PAGES:
            self.app.add_url_rule(
                f"/{self.config.PATH}/{ui}",
                f"doc_page_{ui}",
                lambda ui=ui: PAGES[ui].format(self.config),
            )
