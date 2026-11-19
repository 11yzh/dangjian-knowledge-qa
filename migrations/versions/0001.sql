CREATE TABLE user_account (
 user_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), username varchar(64) NOT NULL, username_key varchar(64) NOT NULL UNIQUE,
 password_hash text NOT NULL, display_name varchar(64) NOT NULL, status text NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','DISABLED')),
 must_change_password boolean NOT NULL DEFAULT true, failed_count int NOT NULL DEFAULT 0 CHECK(failed_count>=0), locked_until timestamptz,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE role(role_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), role_code text NOT NULL UNIQUE);
INSERT INTO role(role_code) VALUES ('USER'),('KNOWLEDGE_ADMIN'),('SYSTEM_ADMIN');
CREATE TABLE user_role(user_id uuid REFERENCES user_account,role_id uuid REFERENCES role,PRIMARY KEY(user_id,role_id));
CREATE TABLE auth_policy(policy_id int PRIMARY KEY CHECK(policy_id=1),max_failures int NOT NULL CHECK(max_failures>0),lock_minutes int NOT NULL CHECK(lock_minutes>0),idle_minutes int NOT NULL CHECK(idle_minutes>0),updated_by uuid REFERENCES user_account,updated_at timestamptz DEFAULT now());
INSERT INTO auth_policy VALUES(1,5,15,30,NULL,now());
CREATE TABLE auth_session(auth_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),user_id uuid NOT NULL REFERENCES user_account,current_role_id uuid NOT NULL REFERENCES role,token_hash char(64) UNIQUE NOT NULL,last_active_at timestamptz NOT NULL DEFAULT now(),expires_at timestamptz NOT NULL);
CREATE INDEX auth_user ON auth_session(user_id);
CREATE TABLE conversation(session_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),owner_id uuid NOT NULL REFERENCES user_account,title varchar(100) NOT NULL DEFAULT '新会话',status text NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','DELETED')),created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),deleted_at timestamptz);
CREATE INDEX conversation_list ON conversation(owner_id,status,updated_at DESC);
CREATE TABLE category(category_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),name varchar(64) NOT NULL,parent_id uuid REFERENCES category);
CREATE UNIQUE INDEX category_names ON category(parent_id,name) NULLS NOT DISTINCT;
CREATE TABLE tag(tag_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),name varchar(32) NOT NULL UNIQUE);
CREATE TABLE knowledge_document(document_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),category_id uuid NOT NULL REFERENCES category,title varchar(200) NOT NULL,source_name varchar(200) NOT NULL,source_url varchar(2048),current_version_id uuid);
CREATE TABLE document_tag(document_id uuid REFERENCES knowledge_document,tag_id uuid REFERENCES tag ON DELETE CASCADE,PRIMARY KEY(document_id,tag_id));
CREATE TABLE document_version(
 version_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),document_id uuid NOT NULL REFERENCES knowledge_document,version_no int NOT NULL CHECK(version_no>=1),file_type text NOT NULL CHECK(file_type IN ('PDF','DOCX','TXT')),file_path text NOT NULL,sha256 char(64) NOT NULL,size_bytes int NOT NULL CHECK(size_bytes BETWEEN 1 AND 20971520),
 state text NOT NULL CHECK(state IN ('UPLOADED','PROCESSING','FAILED','REVIEW_PENDING','REJECTED','PUBLISHED','WITHDRAWN','ARCHIVED')),
 index_state text NOT NULL DEFAULT 'PENDING' CHECK(index_state IN ('PENDING','READY','FAILED')),valid_from date,valid_to date,uploader_id uuid NOT NULL REFERENCES user_account,reviewer_id uuid REFERENCES user_account,review_note varchar(1000),created_at timestamptz NOT NULL DEFAULT now(),reviewed_at timestamptz,published_at timestamptz,revision int NOT NULL DEFAULT 1,parser_version text,index_version text,
 UNIQUE(document_id,version_id),UNIQUE(document_id,version_no),UNIQUE(document_id,sha256),CHECK(valid_to>=valid_from),
 CHECK(state<>'PUBLISHED' OR (reviewer_id IS NOT NULL AND published_at IS NOT NULL AND index_state='READY'))
);
CREATE UNIQUE INDEX one_live_version ON document_version(document_id) WHERE state='PUBLISHED';
ALTER TABLE knowledge_document ADD FOREIGN KEY(document_id,current_version_id) REFERENCES document_version(document_id,version_id) DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE version_role(version_id uuid REFERENCES document_version,role_id uuid REFERENCES role,PRIMARY KEY(version_id,role_id));
CREATE INDEX access_by_role ON version_role(role_id,version_id);
CREATE TABLE ingest_job(job_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),version_id uuid NOT NULL UNIQUE REFERENCES document_version,state text NOT NULL DEFAULT 'QUEUED',stage text NOT NULL DEFAULT '等待处理',attempt int NOT NULL DEFAULT 0,lease_token uuid,lease_until timestamptz,reason_code text,updated_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX job_queue ON ingest_job(state,updated_at);
CREATE TABLE knowledge_chunk(chunk_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),version_id uuid NOT NULL REFERENCES document_version,ordinal int NOT NULL CHECK(ordinal>0),locator varchar(200) NOT NULL,content text NOT NULL,search_vector tsvector NOT NULL,UNIQUE(version_id,ordinal));
CREATE INDEX chunk_fts ON knowledge_chunk USING gin(search_vector);
CREATE TABLE model_config(config_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),provider varchar(16) NOT NULL CHECK(provider IN ('DEEPSEEK','QWEN')),model_name varchar(128) NOT NULL,secret_ref text NOT NULL,status text NOT NULL DEFAULT 'INACTIVE',verified boolean NOT NULL DEFAULT false,timeout_seconds int NOT NULL CHECK(timeout_seconds BETWEEN 5 AND 120),top_k int NOT NULL CHECK(top_k BETWEEN 1 AND 20),threshold numeric(5,4) NOT NULL CHECK(threshold BETWEEN 0 AND 1),context_rounds int NOT NULL CHECK(context_rounds BETWEEN 0 AND 20),context_budget int NOT NULL CHECK(context_budget BETWEEN 1000 AND 32000),updated_by uuid REFERENCES user_account,updated_at timestamptz NOT NULL DEFAULT now());
CREATE UNIQUE INDEX one_active_config ON model_config(status) WHERE status='ACTIVE';
CREATE TABLE request_guard(request_id varchar(64) PRIMARY KEY,actor_id uuid NOT NULL REFERENCES user_account,session_id uuid REFERENCES conversation,role_id uuid REFERENCES role,action varchar(64) NOT NULL,input_digest char(64) NOT NULL,state text NOT NULL CHECK(state IN ('RUNNING','DONE')),config_version uuid REFERENCES model_config,deadline_at timestamptz NOT NULL,result_ref jsonb,created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE message(message_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),session_id uuid NOT NULL REFERENCES conversation,request_id varchar(64) NOT NULL REFERENCES request_guard,speaker text NOT NULL CHECK(speaker IN ('USER','ASSISTANT')),status text NOT NULL,content text,config_version uuid REFERENCES model_config,created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(request_id,speaker));
CREATE INDEX message_list ON message(session_id,created_at);
CREATE TABLE citation(citation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),message_id uuid NOT NULL REFERENCES message ON DELETE CASCADE,chunk_id uuid NOT NULL REFERENCES knowledge_chunk,order_no int NOT NULL CHECK(order_no>0),snippet_snapshot text NOT NULL,locator_snapshot varchar(200) NOT NULL,source_snapshot jsonb NOT NULL,UNIQUE(message_id,order_no));
CREATE TABLE feedback(feedback_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),message_id uuid NOT NULL REFERENCES message ON DELETE CASCADE,user_id uuid NOT NULL REFERENCES user_account,rating text NOT NULL CHECK(rating IN ('USEFUL','NOT_USEFUL')),error_type varchar(32),comment varchar(1000),status text NOT NULL DEFAULT 'PENDING',revision int NOT NULL DEFAULT 1,created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),UNIQUE(message_id,user_id));
CREATE TABLE feedback_action(action_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),feedback_id uuid NOT NULL REFERENCES feedback ON DELETE CASCADE,handler_id uuid NOT NULL REFERENCES user_account,to_status text NOT NULL,note varchar(1000) NOT NULL,new_version_id uuid REFERENCES document_version,created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE safety_rule(rule_id uuid DEFAULT gen_random_uuid(),rule_version int NOT NULL DEFAULT 1,pattern varchar(200) NOT NULL,match_type text NOT NULL CHECK(match_type IN ('EXACT','CONTAINS')),action text NOT NULL CHECK(action IN ('BLOCK','MASK','LOG')),enabled boolean NOT NULL DEFAULT true,allowlist boolean NOT NULL DEFAULT false,description varchar(200) NOT NULL DEFAULT '',updated_by uuid REFERENCES user_account,updated_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(rule_id,rule_version));
CREATE TABLE faq(faq_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),title varchar(100) NOT NULL,question varchar(2000) NOT NULL,category_id uuid NOT NULL REFERENCES category,sort_order int NOT NULL DEFAULT 0 CHECK(sort_order>=0),published boolean NOT NULL DEFAULT false,updated_by uuid REFERENCES user_account,updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE query_event(request_id varchar(64) PRIMARY KEY REFERENCES request_guard,session_id uuid NOT NULL REFERENCES conversation,outcome text NOT NULL CHECK(outcome IN ('SUCCESS','NO_EVIDENCE','REFUSED','SERVICE_ERROR')),reason_code text,latency_ms int NOT NULL CHECK(latency_ms>=0),topic varchar(100) NOT NULL,occurred_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX events_day ON query_event(occurred_at,outcome);
CREATE TABLE audit_log(audit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),actor_id uuid REFERENCES user_account,request_id varchar(64),action varchar(64) NOT NULL,target varchar(200) NOT NULL,result text NOT NULL CHECK(result IN ('SUCCESS','FAILURE')),summary varchar(1000) NOT NULL,occurred_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX audit_date ON audit_log(occurred_at,actor_id);
CREATE INDEX audit_request ON audit_log(request_id);
