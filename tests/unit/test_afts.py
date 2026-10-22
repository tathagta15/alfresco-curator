from alfresco_curator.alfresco import afts


def test_splits_top_level_and_only():
    query = (
        'TYPE:"cm:content" AND cm:name:"*spec*" '
        "AND cm:modified:[2026-08-01T00:00:00.000Z TO NOW] "
        'AND (cm:title:"S3" OR cm:description:"S3")'
    )
    assert afts.split_top_level_and(query) == [
        'TYPE:"cm:content"',
        'cm:name:"*spec*"',
        "cm:modified:[2026-08-01T00:00:00.000Z TO NOW]",
        '(cm:title:"S3" OR cm:description:"S3")',
    ]


def test_does_not_split_inside_quotes():
    query = 'cm:title:"budget AND forecast" AND TYPE:"cm:content"'
    assert afts.split_top_level_and(query) == [
        'cm:title:"budget AND forecast"',
        'TYPE:"cm:content"',
    ]


def test_escaped_colon_survives():
    query = r'@cm\:name:"report" AND TYPE:"cm:content"'
    assert afts.split_top_level_and(query) == [
        r'@cm\:name:"report"',
        'TYPE:"cm:content"',
    ]


def test_top_level_or_is_not_isolated():
    query = 'TYPE:"cm:content" OR TYPE:"cm:folder"'
    assert afts.split_top_level_and(query) == [query]
    assert not afts.is_pure_conjunction(query)


def test_and_not_is_not_isolated():
    query = 'TYPE:"cm:content" AND NOT ASPECT:"cm:titled"'
    assert afts.split_top_level_and(query) == [query]
    assert not afts.is_pure_conjunction(query)


def test_scope_to_node():
    assert afts.scope_to_node('TYPE:"cm:content"', "abc-123") == (
        '(TYPE:"cm:content") AND ID:"workspace://SpacesStore/abc-123"'
    )
