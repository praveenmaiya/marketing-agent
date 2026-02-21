"""Tests for the agent tool system."""



from src.agent.tools import ToolDef, ToolRegistry


class TestToolDef:
    def test_to_api_schema(self):
        tool = ToolDef(
            name="test_tool",
            description="A test tool",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            handler=lambda p: "result",
        )

        schema = tool.to_api_schema()
        assert schema["name"] == "test_tool"
        assert schema["description"] == "A test tool"
        assert schema["input_schema"]["type"] == "object"
        assert "query" in schema["input_schema"]["properties"]

    def test_execute_success(self):
        tool = ToolDef(
            name="echo",
            description="Echo input",
            input_schema={"type": "object", "properties": {}},
            handler=lambda p: f"echo: {p.get('msg', '')}",
        )
        result = tool.execute({"msg": "hello"})
        assert result == "echo: hello"

    def test_execute_error_returns_error_string(self):
        def failing_handler(params):
            raise ValueError("something broke")

        tool = ToolDef(
            name="failing",
            description="Always fails",
            input_schema={"type": "object", "properties": {}},
            handler=failing_handler,
        )
        result = tool.execute({})
        assert "failed" in result
        assert "ValueError" in result
        assert "something broke" in result


class TestToolRegistry:
    def test_register_and_get(self):
        registry = ToolRegistry()
        tool = ToolDef(
            name="my_tool",
            description="test",
            input_schema={"type": "object", "properties": {}},
            handler=lambda p: "ok",
        )
        registry.register(tool)
        assert registry.get("my_tool") is tool
        assert registry.get("nonexistent") is None

    def test_list_tools(self):
        registry = ToolRegistry()
        for i in range(3):
            registry.register(ToolDef(
                name=f"tool_{i}",
                description=f"Tool {i}",
                input_schema={"type": "object", "properties": {}},
                handler=lambda p: "ok",
            ))
        tools = registry.list_tools()
        assert len(tools) == 3

    def test_to_api_schemas(self):
        registry = ToolRegistry()
        registry.register(ToolDef(
            name="query",
            description="Run SQL",
            input_schema={
                "type": "object",
                "properties": {"sql": {"type": "string"}},
            },
            handler=lambda p: "result",
        ))
        schemas = registry.to_api_schemas()
        assert len(schemas) == 1
        assert schemas[0]["name"] == "query"

    def test_register_overwrites(self):
        registry = ToolRegistry()
        tool1 = ToolDef(
            name="x",
            description="version 1",
            input_schema={"type": "object", "properties": {}},
            handler=lambda p: "v1",
        )
        tool2 = ToolDef(
            name="x",
            description="version 2",
            input_schema={"type": "object", "properties": {}},
            handler=lambda p: "v2",
        )
        registry.register(tool1)
        registry.register(tool2)
        assert registry.get("x").description == "version 2"
        assert len(registry.list_tools()) == 1
