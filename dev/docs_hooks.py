"""MkDocs hooks for the docs site (mkdocs.yml): pages kept elsewhere in the repo, or made from the code, so the site
can't drift from what the server does.

- Reference / Agent guides: knowledge/guides/*.md, the guides the server gives agents (stroom://guide/{name}), as
  they are. They stay where the server reads them; the site shows them.
- Reference / Tools, prompts and resources: generated from the tools as an MCP client lists them (FastMCP renders
  each from its function: the description and the parameters' JSON schema), and the prompts and resources the
  server registers.
"""
import asyncio
import sys
from pathlib import Path

from mkdocs.structure.files import File, Files

ROOT = Path(__file__).resolve().parents[1]
GUIDES = ROOT / 'knowledge' / 'guides'
# The tool groups' names, in the order an onboarding uses them; a module not named here comes after them, under its
# docstring's first line.
PARAGRAPH = '\n\n'
GROUPS = {'plan': 'The plan', 'explorer': 'Finding documents', 'templates': 'Template pipelines',
          'feeds': 'Samples and feeds', 'translation': 'Translation content', 'generation': 'Generation',
          'pipeline_writes': 'Pipelines', 'stepping': 'Stepping', 'validation': 'Validation',
          'processing_writes': 'Processing', 'processing': 'Processing status', 'streams': 'Streams and errors',
          'indexing': 'Indexing', 'builds': 'Builds and promotion', 'sampling': 'Sampling an existing feed',
          'coverage': 'Coverage', 'diagnosis': 'Diagnosis', 'rebuild': 'Rebuilding a mapping',
          'reference': 'Reference data', 'instructions': 'Standing instructions', 'cef': 'CEF output',
          'describe': 'Questions about the data'}


def on_files(files: Files, config) -> Files:
    for path in sorted(GUIDES.glob('*.md')):
        files.append(File.generated(config, f'reference/guides/{path.name}', abs_src_path=str(path)))
    files.append(File.generated(config, 'reference/tools.md', content=tools_page()))
    return files


def on_page_markdown(markdown: str, page, config, files) -> str:
    if page.file.src_uri.startswith('reference/guides/'):
        name = Path(page.file.src_uri).stem
        note = (f'!!! note "An agent guide"\n    The server gives this guide to agents as `stroom://guide/{name}`; it is '
                f'shown here as they read it. Source: [knowledge/guides/{name}.md]'
                f'(https://github.com/gradata-systems/stroom-fastmcp-server/blob/master/knowledge/guides/{name}.md).\n\n')
        first, _, rest = markdown.partition('\n')
        return f'{first}\n\n{note}{rest}' if first.startswith('# ') else note + markdown
    return markdown


def _schema_type(spec: dict) -> str:
    if 'anyOf' in spec:
        kinds = [_schema_type(s) for s in spec['anyOf'] if s.get('type') != 'null']
        return ' or '.join(dict.fromkeys(kinds)) or 'any'
    if spec.get('enum'):
        return ' \\| '.join(f'`{v}`' for v in spec['enum'])
    kind = spec.get('type', 'object' if '$ref' in spec or 'properties' in spec else 'any')
    if kind == 'array':
        return f"list of {_schema_type(spec.get('items', {}))}"
    return kind


def _cell(text: str) -> str:
    return ' '.join((text or '').split()).replace('|', '\\|')


def tools_page() -> str:
    sys.path.insert(0, str(ROOT))
    from fastmcp import FastMCP
    from fastmcp.tools import Tool
    from main_tools import TOOL_MODULES
    from tools import resources

    lines = ['# Tools, prompts and resources', '',
             'Generated from the server itself: each tool as an MCP client lists it, with the description the agent '
             'reads and its parameters. Tools marked **confirmation** or **approval** ask the user in a form before '
             'they act (see [Confirmations and approvals](../operate/confirmations.md)).', '']
    mcp = FastMCP('docs')
    resources.register(mcp)
    prompts = asyncio.run(mcp.list_prompts())
    templates = asyncio.run(mcp.list_resource_templates())
    listed = asyncio.run(mcp.list_resources())

    lines += ['## Prompts', '', 'The workflows, which a client may offer as slash commands.', '',
              '| Prompt | What it does | Arguments |', '| --- | --- | --- |']
    for p in prompts:
        args = ', '.join(f'`{a.name}`' + ('' if a.required else ' (optional)') for a in p.arguments or [])
        lines.append(f'| `{p.name}` | {_cell(p.description)} | {args} |')
    lines += ['', '## Resources', '', '| URI | What it is |', '| --- | --- |']
    for r in listed:
        lines.append(f'| `{r.uri}` | {_cell(r.description)} |')
    for r in templates:
        lines.append(f'| `{r.uri_template}` | {_cell(r.description)} |')
    lines.append('')

    order = list(GROUPS)
    modules = sorted((m for m in TOOL_MODULES if m.ALL_TOOLS),
                     key=lambda m: order.index(m.__name__.split('.')[-1]) if m.__name__.split('.')[-1] in order else len(order))
    for module in modules:
        key = module.__name__.split('.')[-1]
        about = ' '.join((module.__doc__ or '').strip().split(PARAGRAPH)[0].split())
        title = GROUPS.get(key) or about.split('. ')[0].rstrip('.') or key
        lines += [f'## {title}', ''] + ([about, ''] if about else [])
        for fn in module.ALL_TOOLS:
            tool = Tool.from_function(fn)
            gate = _gate(tool.parameters)
            lines += [f'### `{tool.name}`' + (f' ({gate})' if gate else ''), '', (tool.description or '').strip(), '']
            props = tool.parameters.get('properties', {})
            required = set(tool.parameters.get('required', []))
            if props:
                lines += ['| Parameter | Type | Description |', '| --- | --- | --- |']
                for name, spec in props.items():
                    req = '' if name in required else ' (optional)'
                    lines.append(f'| `{name}`{req} | {_schema_type(spec)} | {_cell(spec.get("description", ""))} |')
                lines.append('')
    return '\n'.join(lines) + '\n'


def _gate(parameters: dict) -> str:
    """Whether a tool asks the user first: it takes the answer back as confirmation_id or approval_id."""
    props = parameters.get('properties', {})
    kinds = [k for k in ('approval', 'confirmation') if f'{k}_id' in props]
    return ', '.join(f'**{k}**' for k in kinds)
