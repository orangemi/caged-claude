"""Safe YAML loading for the application and node files."""


class YamlError(ValueError):
    pass


def load(path):
    try:
        import yaml
    except ImportError:
        raise YamlError('PyYAML is required to read the configuration. Use ./start (it runs in the '
                        'launcher container), or install PyYAML for CAGED_NATIVE=1.') from None
    try:
        with open(path, encoding='utf-8') as source:
            # safe_load builds only plain data (no custom tags); aliases are allowed.
            return yaml.safe_load(source)
    except (OSError, yaml.YAMLError):
        raise YamlError('unreadable') from None
