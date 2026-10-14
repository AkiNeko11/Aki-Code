from aki_code.tools.registry import ToolRegistry
from aki_code.tools.bash import BashTool
from aki_code.tools.file_read import ReadFileTool
from aki_code.tools.file_write import WriteFileTool
from aki_code.tools.file_edit import EditFileTool
from aki_code.tools.glob import GlobTool
from aki_code.tools.todo_write import TodoWriteTool
from aki_code.todo.manager import TodoManager


def create_registry(todo_manager: TodoManager) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(BashTool())
    reg.register(ReadFileTool())
    reg.register(WriteFileTool())
    reg.register(EditFileTool())
    reg.register(GlobTool())
    reg.register(TodoWriteTool(todo_manager))
    return reg
