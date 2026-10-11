from aki_code.tools.registry import ToolRegistry
from aki_code.tools.bash import BashTool
from aki_code.tools.file_read import ReadFileTool
from aki_code.tools.file_write import WriteFileTool
from aki_code.tools.file_edit import EditFileTool
from aki_code.tools.glob import GlobTool


def create_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(BashTool())
    reg.register(ReadFileTool())
    reg.register(WriteFileTool())
    reg.register(EditFileTool())
    reg.register(GlobTool())
    return reg
