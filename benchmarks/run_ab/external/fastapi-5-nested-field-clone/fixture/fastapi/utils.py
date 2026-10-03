class ModelType:
    def __init__(self, fields=None):
        self.__fields__ = fields or {}


class ModelField:
    def __init__(self, name, field_type=None):
        self.name = name
        self.type_ = field_type


def create_cloned_field(field):
    use_type = None
    if field.type_ is not None:
        original_type = field.type_
        use_type = ModelType()
        for f in original_type.__fields__.values():
            use_type.__fields__[f.name] = f
    return ModelField(field.name, use_type)
