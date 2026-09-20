Please analyze the following user request and create a detailed task list (Todo List).
User Request: {user_input}

Use the create_todo_list tool to generate the task list.

IMPORTANT PARALLEL EXECUTION RULES:
- Tasks WITHOUT dependencies on each other will be executed IN PARALLEL by multiple workers simultaneously
- Only add dependencies when a task TRULY needs another task's output
- Maximize parallelism by minimizing unnecessary dependencies
- Each task description should be sufficiently detailed for the Worker Agent to complete independently

Example of good parallel design:
- "Search for info about X" and "Search for info about Y" → NO dependencies (parallel)
- "Write report based on search results" → depends on both search tasks (sequential after them)
- 