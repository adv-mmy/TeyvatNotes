-- 授权与字段证据属于特定文档版本，不复用同 URL 其他版本的许可。
ALTER TABLE guide_version ADD COLUMN web_evidence jsonb;
ALTER TABLE guide_version ADD CONSTRAINT guide_web_evidence_valid CHECK (
    web_evidence IS NULL OR (
        web_evidence ?& ARRAY['permission', 'parser', 'fields'] AND
        length(web_evidence->>'permission') > 0 AND
        (web_evidence->'fields') ?& ARRAY['entity', 'versions', 'kind', 'conditions']
    )
);
-- 不加入检索视图或索引检查点。相同来源和内容不重复归档。
CREATE TABLE guide_incomplete_page (
    source_url text NOT NULL, content_hash text NOT NULL,
    permission_ref text NOT NULL, title text NOT NULL, content text NOT NULL,
    field_proofs jsonb NOT NULL, reasons text[] NOT NULL,
    PRIMARY KEY (source_url, content_hash, permission_ref)
);
