from tavily.databases.mongodb.mongodb import MongoDB
from tavily.databases.base import Database

_LEGACY_POSITIONAL_ARGS = (
    "collection",
    "index",
    "embeddings_field",
    "content_field",
    "embedding_function",
    "ranking_function",
    "session",
)

_NEW_POSITIONAL_ARGS = (
    "embedding_function",
    "ranking_function",
    "session",
)

_LEGACY_DATABASE_OPTIONS = {
    "db_provider",
    "collection",
    "index",
    "embeddings_field",
    "content_field"
}

def _parse_positional_args(args, *, api_name):
    # assert(api_name == "legacy" or api_name == "new", "'api_name' can be either 'legacy' or 'new'")
    names = _LEGACY_POSITIONAL_ARGS if api_name == "legacy" else _NEW_POSITIONAL_ARGS

    if len(args) > len(names):
        raise TypeError(
            f"TavilyHybridClient() takes at most "
            f"{2 + len(names)} positional arguments "
            f"for the {api_name} API, but "
            f"{2 + len(args)} were given"
        )

    return dict(zip(names, args))


_UNSET = object() # For handling cases, when user passes this in new API
# TavilyHybridClient(
#   api_key,
#   database,
#   embed_fn,
#   embedding_function=None, 
#)
# With default of None, we can't distinguish it from omission
def _resolve_constructor_arguments(
    database,
    args,
    embedding_function,
    ranking_function,
    session,
    legacy_database_options,
):
    # Legacy/New API
    unknown = (
        legacy_database_options.keys()
        - _LEGACY_DATABASE_OPTIONS
    )

    if unknown:
        names = ", ".join(sorted(repr(name) for name in unknown))
        raise TypeError(
            f"Unexpected database option(s): {names}. "
            "For the new API, create a MongoDB or OracleDB database object "
            "and pass it using `database=`."
        )

    if isinstance(database, str):
        if "db_provider" in legacy_database_options:
            raise TypeError(
                "TavilyHybridClient() got multiple values for argument 'db_provider'"
            )
        
        legacy_args = _parse_positional_args(
            args,
            api_name="legacy",
        )

        duplicates = legacy_args.keys() & legacy_database_options.keys()
        if duplicates:
            name = next(iter(duplicates))
            raise TypeError(
                f"TavilyHybridClient() got multiple values for argument {name!r}"
            )

        for name, keyword_value in (
            ("embedding_function", embedding_function),
            ("ranking_function", ranking_function),
            ("session", session),
        ):
            if name in legacy_args and keyword_value is not _UNSET:
                raise TypeError(
                    f"TavilyHybridClient() got multiple values "
                    f"for argument {name!r}"
                )

        legacy_args.update(legacy_database_options)
        if "embedding_function" in legacy_args:
            embedding_function = legacy_args.pop(
                "embedding_function"
            )

        if "ranking_function" in legacy_args:
            ranking_function = legacy_args.pop(
                "ranking_function"
            )

        if "session" in legacy_args:
            session = legacy_args.pop("session")

        database = create_legacy_database({
            "db_provider": database,
            **legacy_args,
        })

    elif isinstance(database, Database):
        new_args = _parse_positional_args(
            args,
            api_name="new",
        )

        for name, keyword_value in (
            ("embedding_function", embedding_function),
            ("ranking_function", ranking_function),
            ("session", session),
        ):
            if name in new_args and keyword_value is not _UNSET:
                raise TypeError(
                    f"TavilyHybridClient() got multiple values for argument {name!r}"
                )
            
        if legacy_database_options:
            unexpected = ", ".join(
                sorted(legacy_database_options)
            )
            raise TypeError(
                "database cannot be combined with legacy "
                f"database options: {unexpected}."
            )
        
        if "embedding_function" in new_args:
            embedding_function = new_args["embedding_function"]

        if "ranking_function" in new_args:
            ranking_function = new_args["ranking_function"]

        if "session" in new_args:
            session = new_args["session"]
    elif database is not None:
        raise TypeError(
            "database must be a configured MongoDB or OracleDB instance. "
            "For the legacy API, pass 'mongodb' as the database provider."
        )
    else:
        if args:
            raise TypeError(
                "Unexpected positional arguments when `database` is None. "
                "For the legacy API, pass 'mongodb' as the database provider. "
                "For the new API, pass a configured Database instance."
            )
        
        database = create_legacy_database(
            legacy_database_options
        )

    # Convert the internal sentinel back to the public API defaults.
    if embedding_function is _UNSET:
        embedding_function = None

    if ranking_function is _UNSET:
        ranking_function = None

    if session is _UNSET:
        session = None

    return (
        database,
        embedding_function,
        ranking_function,
        session,
    )

def create_legacy_database(options):
    """Build a database adapter from the pre-adapter client keyword arguments.

    This compatibility bridge keeps existing applications working while new
    code passes a configured ``MongoDB`` or ``OracleDB`` object to
    ``TavilyHybridClient``. It deliberately belongs in the database package so
    the shared client does not contain provider-specific construction logic.
    """
    options = dict(options)
    provider = options.pop("db_provider", None)

    if provider == "mongodb":
        retrieval_mode = options.get("retrieval_mode", "hybrid_search")
        if retrieval_mode in ("freshness_cache", "cache_then_memory"):
            raise ValueError(
                "retrieval_mode='freshness_cache' and "
                "retrieval_mode='cache_then_memory' are only supported when "
                "db_provider='oracle'."
            )
        return MongoDB(
            collection=options.get("collection"),
            index=options.get("index"),
            embeddings_field=options.get("embeddings_field", "embeddings"),
            content_field=options.get("content_field", "content"),
        )
    elif provider == "oracledb":
        raise ValueError(
            "Only MongoDB can be created via legacy API"
        )

    raise ValueError(
        "Database is required. Pass a configured MongoDB or OracleDB "
        "instance, or use db_provider='mongodb' with the legacy API."
    )
