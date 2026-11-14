"""Unit tests of the SQL statement splitter used by orchestration/run_pipeline.py."""
from __future__ import annotations

import pytest

from orchestration.run_pipeline import render, split_sql


def test_splits_simple_statements():
    assert split_sql("SELECT 1; SELECT 2;") == ["SELECT 1", "SELECT 2"]


def test_ignores_comments():
    sql = "-- comment ; with semicolon\nSELECT 1; /* block ; comment */ SELECT 2"
    assert split_sql(sql) == ["SELECT 1", "SELECT 2"]


def test_keeps_semicolons_inside_strings_and_identifiers():
    sql = "SELECT 'a;b', 'it''s;' AS \"x;y\"; SELECT 3;"
    assert split_sql(sql) == ["SELECT 'a;b', 'it''s;' AS \"x;y\"", "SELECT 3"]


def test_keeps_procedure_body_together():
    sql = """CREATE PROCEDURE P() RETURNS VARCHAR LANGUAGE SQL AS
$$
BEGIN
    INSERT INTO T VALUES (1);  -- not a split point
    RETURN 'ok';
END;
$$;
CALL P();"""
    parts = split_sql(sql)
    assert len(parts) == 2
    assert parts[0].count("$$") == 2 and "RETURN 'ok';" in parts[0]
    assert parts[1] == "CALL P()"


def test_unterminated_quote_raises():
    with pytest.raises(ValueError):
        split_sql("SELECT 'oops;")


def test_render_placeholders():
    assert render("FORCE = {{TB_FORCE}} '{{LOAD_ID}}'", {"TB_FORCE": "TRUE", "LOAD_ID": "L1"}) == "FORCE = TRUE 'L1'"
