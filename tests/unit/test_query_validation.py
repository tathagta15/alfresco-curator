from alfresco_curator.diagnostics.query_generator import path_problems


def test_valid_descendant_path():
    assert path_problems('PATH:"/app:company_home/cm:Engineering//*"') == []


def test_valid_children_path():
    assert path_problems('PATH:"/app:company_home/st:sites/cm:demo/*"') == []


def test_query_without_path_is_fine():
    assert path_problems('TYPE:"cm:content" AND cm:name:"*spec*"') == []


def test_wildcard_before_named_step_is_rejected():
    problems = path_problems('PATH:"/app:company_home//*cm:Engineering//*"')
    assert problems and "wildcard" in problems[0]


def test_display_path_without_prefixes_is_rejected():
    problems = path_problems('PATH:"/Company Home/Engineering"')
    assert problems and "namespace prefix" in problems[0]


def test_relative_path_is_rejected():
    problems = path_problems('PATH:"app:company_home/cm:Engineering"')
    assert problems and "must start with /" in problems[0]
