def tqdm_auto(iterable, *args, **kwargs):
    return iterable


def tenumerate(iterable, start=0, total=None, tqdm_class=tqdm_auto, **tqdm_kwargs):
    return enumerate(tqdm_class(iterable, start, **tqdm_kwargs))
