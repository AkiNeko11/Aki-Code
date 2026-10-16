from aki_code.tools.registry import ToolRegistry
from aki_code.tools.bash import BashTool
from aki_code.tools.file_read import ReadFileTool
from aki_code.tools.file_write import WriteFileTool
from aki_code.tools.file_edit import EditFileTool
from aki_code.tools.glob import GlobTool
from aki_code.tools.todo_write import TodoWriteTool
from aki_code.tools.load_skill import LoadSkillTool
from aki_code.tools.compact import CompactTool
from aki_code.tools.task import TaskTool
from aki_code.todo.manager import TodoManager
from aki_code.skills.loader import SkillLoader


def create_registry(
    *,
    client,
    permission_manager,
    hook_manager,
    todo_manager: TodoManager,
    skill_loader: SkillLoader,
    ask_permission,
    console,
) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(BashTool())
    reg.register(ReadFileTool())
    reg.register(WriteFileTool())
    reg.register(EditFileTool())
    reg.register(GlobTool())
    reg.register(TodoWriteTool(todo_manager))
    reg.register(LoadSkillTool(skill_loader))
    reg.register(CompactTool())
    reg.register(TaskTool(
        client=client,
        registry=reg,
        permission_manager=permission_manager,
        hook_manager=hook_manager,
        ask_permission=ask_permission,
        console=console,
    ))
    return reg
