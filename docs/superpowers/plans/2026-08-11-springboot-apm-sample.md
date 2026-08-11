# Sample Spring Boot EC2 APM Target — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a small Spring Boot To-Do/Task CRUD app with three intentional backend bugs, deployable to a private-subnet EC2 in the same account/region as DBOps, with CloudWatch Agent shipping JSON logs + host metrics, so the DBOps `/apm` dashboard can track the bugs.

**Architecture:** Spring Boot 3 (Java 17, Maven fat jar) → runs under systemd on a private EC2 (no public IP, NAT egress) → Logback writes **JSON** log lines to a file → CloudWatch Agent tails the file to Log Group `/dbops/apm/todoapp` and publishes CPU/mem/disk. A VPC-internal load-generator Lambda drives mostly-healthy traffic plus a trickle of bug triggers on a 2-minute schedule. Everything lives under `samples/springboot/` in its own CDK app.

**Tech Stack:** Java 17, Spring Boot 3.2.x, Spring Data JPA, H2 (in-memory), Logback + `logstash-logback-encoder` (JSON), Maven; AWS CDK (Python 3.9), EC2 Amazon Linux 2023, CloudWatch Agent, Lambda (Python 3.12), EventBridge.

## Global Constraints

- **No public inbound, ever.** App EC2 has no public IP; its security group has **no inbound rules** (egress only). Access via SSM Session Manager only.
- **Same account/region as DBOps:** account `571850511781`, region `us-east-1`. APM target registered later with `spoke_role_arn` blank (local session, no cross-account).
- **JSON logging** to `/var/log/todoapp/app.log`. Each line is a JSON object with at least a `level` field (UPPERCASE: `ERROR`/`WARN`/`INFO`) and a `message` field. This satisfies both the APM log **search** (`@message like /ERROR/`) and the collector's level-**count** (`stats count(*) by level`, needs a `level` field).
- **Log Group name:** `/dbops/apm/todoapp` (created by CDK, retention 7 days).
- **Not wired into the platform test suite** (`tests/unit`, `tests/cdk`, parity). `samples/` is managed separately.
- Standalone CDK app: `samples/springboot/cdk/cdk.json` + `app.py`, so `cdk deploy` works without the main app (avoids the known `samples/cdk` limitation).
- App listens on `:8080`. Stack name: `dbops-dev-springboot-apm`.
- Bug rule: normal traffic returns 200. Bugs fire only on specific inputs/endpoints, at a low rate from the load generator.

---

### Task 1: Spring Boot project skeleton + Task entity/repository

**Files:**
- Create: `samples/springboot/app/pom.xml`
- Create: `samples/springboot/app/src/main/java/com/dbops/todo/TodoApplication.java`
- Create: `samples/springboot/app/src/main/java/com/dbops/todo/Task.java`
- Create: `samples/springboot/app/src/main/java/com/dbops/todo/TaskRepository.java`
- Create: `samples/springboot/app/src/main/resources/application.yml`
- Test: `samples/springboot/app/src/test/java/com/dbops/todo/TaskRepositoryTest.java`

**Interfaces:**
- Produces: `Task` entity (fields `Long id`, `String title` UNIQUE NOT NULL, `boolean done`, `String note` nullable); `TaskRepository extends JpaRepository<Task, Long>` with `Optional<Task> findByTitle(String title)`.

- [ ] **Step 1: Write `pom.xml`**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>3.2.5</version>
    <relativePath/>
  </parent>
  <groupId>com.dbops</groupId>
  <artifactId>todoapp</artifactId>
  <version>1.0.0</version>
  <packaging>jar</packaging>
  <properties>
    <java.version>17</java.version>
  </properties>
  <dependencies>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-web</artifactId>
    </dependency>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-data-jpa</artifactId>
    </dependency>
    <dependency>
      <groupId>com.h2database</groupId>
      <artifactId>h2</artifactId>
      <scope>runtime</scope>
    </dependency>
    <dependency>
      <groupId>net.logstash.logback</groupId>
      <artifactId>logstash-logback-encoder</artifactId>
      <version>7.4</version>
    </dependency>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-test</artifactId>
      <scope>test</scope>
    </dependency>
  </dependencies>
  <build>
    <finalName>todoapp</finalName>
    <plugins>
      <plugin>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-maven-plugin</artifactId>
      </plugin>
    </plugins>
  </build>
</project>
```

- [ ] **Step 2: Write `TodoApplication.java`**

```java
package com.dbops.todo;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class TodoApplication {
    public static void main(String[] args) {
        SpringApplication.run(TodoApplication.class, args);
    }
}
```

- [ ] **Step 3: Write `Task.java`**

```java
package com.dbops.todo;

import jakarta.persistence.*;

@Entity
@Table(name = "tasks", uniqueConstraints = @UniqueConstraint(columnNames = "title"))
public class Task {
    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(nullable = false, unique = true)
    private String title;

    private boolean done;

    private String note;

    public Task() {}

    public Task(String title, boolean done, String note) {
        this.title = title;
        this.done = done;
        this.note = note;
    }

    public Long getId() { return id; }
    public void setId(Long id) { this.id = id; }
    public String getTitle() { return title; }
    public void setTitle(String title) { this.title = title; }
    public boolean isDone() { return done; }
    public void setDone(boolean done) { this.done = done; }
    public String getNote() { return note; }
    public void setNote(String note) { this.note = note; }
}
```

- [ ] **Step 4: Write `TaskRepository.java`**

```java
package com.dbops.todo;

import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;

public interface TaskRepository extends JpaRepository<Task, Long> {
    Optional<Task> findByTitle(String title);
}
```

- [ ] **Step 5: Write `application.yml`**

```yaml
spring:
  datasource:
    url: jdbc:h2:mem:tododb;DB_CLOSE_DELAY=-1
    driver-class-name: org.h2.Driver
    username: sa
    password: ""
  jpa:
    hibernate:
      ddl-auto: create-drop
    properties:
      hibernate.format_sql: false
server:
  port: 8080
```

- [ ] **Step 6: Write the failing repository test**

```java
package com.dbops.todo;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.orm.jpa.DataJpaTest;

@DataJpaTest
class TaskRepositoryTest {
    @Autowired
    TaskRepository repo;

    @Test
    void savesAndFindsByTitle() {
        repo.save(new Task("buy milk", false, null));
        assertThat(repo.findByTitle("buy milk")).isPresent();
        assertThat(repo.findByTitle("nope")).isEmpty();
    }
}
```

- [ ] **Step 7: Run test — expect PASS**

Run: `cd samples/springboot/app && mvn -q test -Dtest=TaskRepositoryTest`
Expected: build downloads deps, test PASSES. (If Maven is unavailable in this environment, note it and defer to CI/deploy host; the code is still committed.)

- [ ] **Step 8: Commit**

```bash
git add samples/springboot/app/pom.xml samples/springboot/app/src
git commit -m "feat(samples): Spring Boot todo skeleton (entity + repo)"
```

---

### Task 2: JSON logging config + global exception handler

**Files:**
- Create: `samples/springboot/app/src/main/resources/logback-spring.xml`
- Create: `samples/springboot/app/src/main/java/com/dbops/todo/GlobalExceptionHandler.java`

**Interfaces:**
- Produces: JSON log lines with `level`/`message` fields to `${LOG_DIR:-/var/log/todoapp}/app.log`; `GlobalExceptionHandler` `@RestControllerAdvice` mapping uncaught exceptions to HTTP 500 and logging an ERROR with stack trace.
- Consumes: nothing new.

- [ ] **Step 1: Write `logback-spring.xml`**

Uses a file appender with the logstash JSON encoder so each line has a `level` field (satisfies the collector's `stats count() by level`) and a console appender for `journalctl`. `LOG_DIR` overridable; defaults to `/var/log/todoapp` (created by user-data), falls back for local test runs via the `logdir` property.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<configuration>
  <property name="LOG_DIR" value="${LOG_DIR:-/var/log/todoapp}"/>

  <appender name="CONSOLE" class="ch.qos.logback.core.ConsoleAppender">
    <encoder>
      <pattern>%d{ISO8601} [%level] %logger{0} - %msg%n</pattern>
    </encoder>
  </appender>

  <appender name="JSON_FILE" class="ch.qos.logback.core.rolling.RollingFileAppender">
    <file>${LOG_DIR}/app.log</file>
    <rollingPolicy class="ch.qos.logback.core.rolling.SizeAndTimeBasedRollingPolicy">
      <fileNamePattern>${LOG_DIR}/app.%d{yyyy-MM-dd}.%i.log</fileNamePattern>
      <maxFileSize>10MB</maxFileSize>
      <maxHistory>3</maxHistory>
      <totalSizeCap>50MB</totalSizeCap>
    </rollingPolicy>
    <encoder class="net.logstash.logback.encoder.LogstashEncoder">
      <fieldNames>
        <level>level</level>
        <message>message</message>
        <logger>logger</logger>
      </fieldNames>
    </encoder>
  </appender>

  <root level="INFO">
    <appender-ref ref="CONSOLE"/>
    <appender-ref ref="JSON_FILE"/>
  </root>
</configuration>
```

- [ ] **Step 2: Write `GlobalExceptionHandler.java`**

```java
package com.dbops.todo;

import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

@RestControllerAdvice
public class GlobalExceptionHandler {
    private static final Logger log = LoggerFactory.getLogger(GlobalExceptionHandler.class);

    @ExceptionHandler(DataIntegrityViolationException.class)
    public ResponseEntity<Map<String, String>> handleConstraint(DataIntegrityViolationException ex) {
        log.error("DB constraint violation while saving task", ex);
        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)
                .body(Map.of("error", "constraint_violation", "detail", String.valueOf(ex.getMostSpecificCause().getMessage())));
    }

    @ExceptionHandler(Exception.class)
    public ResponseEntity<Map<String, String>> handleAny(Exception ex) {
        log.error("Unhandled exception in request", ex);
        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)
                .body(Map.of("error", "internal_error", "detail", String.valueOf(ex.getMessage())));
    }
}
```

- [ ] **Step 3: Commit**

```bash
git add samples/springboot/app/src/main/resources/logback-spring.xml \
        samples/springboot/app/src/main/java/com/dbops/todo/GlobalExceptionHandler.java
git commit -m "feat(samples): JSON file logging + global 500 handler"
```

---

### Task 3: TaskController — CRUD + health + NPE bug + constraint bug

**Files:**
- Create: `samples/springboot/app/src/main/java/com/dbops/todo/TaskController.java`
- Create: `samples/springboot/app/src/main/java/com/dbops/todo/TaskRequest.java`
- Test: `samples/springboot/app/src/test/java/com/dbops/todo/TaskControllerTest.java`

**Interfaces:**
- Consumes: `Task`, `TaskRepository`, `GlobalExceptionHandler`.
- Produces: REST routes `GET /api/health`, `GET /api/tasks`, `GET /api/tasks/{id}`, `POST /api/tasks`, `PUT /api/tasks/{id}`, `DELETE /api/tasks/{id}`. **Bug 1 (NPE):** POST body with `note` set and `title` null reaches `title.trim()` → NPE → 500. **Bug 2 (constraint):** POST with a duplicate `title` → `DataIntegrityViolationException` → 500 (no pre-check by design).

- [ ] **Step 1: Write `TaskRequest.java`**

```java
package com.dbops.todo;

public class TaskRequest {
    public String title;
    public Boolean done;
    public String note;
}
```

- [ ] **Step 2: Write the failing controller test**

```java
package com.dbops.todo;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;

@SpringBootTest
@AutoConfigureMockMvc
class TaskControllerTest {
    @Autowired
    MockMvc mvc;

    @Test
    void healthIsUp() throws Exception {
        mvc.perform(get("/api/health")).andExpect(status().isOk())
           .andExpect(jsonPath("$.status").value("UP"));
    }

    @Test
    void createAndListTask() throws Exception {
        mvc.perform(post("/api/tasks").contentType(MediaType.APPLICATION_JSON)
                .content("{\"title\":\"write plan\",\"done\":false}"))
           .andExpect(status().isOk());
        mvc.perform(get("/api/tasks")).andExpect(status().isOk())
           .andExpect(jsonPath("$[0].title").value("write plan"));
    }

    @Test
    void nullTitleWithNoteTriggersNpe500() throws Exception {
        mvc.perform(post("/api/tasks").contentType(MediaType.APPLICATION_JSON)
                .content("{\"note\":\"orphan note\"}"))
           .andExpect(status().isInternalServerError());
    }

    @Test
    void duplicateTitleTriggersConstraint500() throws Exception {
        mvc.perform(post("/api/tasks").contentType(MediaType.APPLICATION_JSON)
                .content("{\"title\":\"dup\"}")).andExpect(status().isOk());
        mvc.perform(post("/api/tasks").contentType(MediaType.APPLICATION_JSON)
                .content("{\"title\":\"dup\"}")).andExpect(status().isInternalServerError());
    }
}
```

- [ ] **Step 3: Run test — expect FAIL (no controller yet)**

Run: `cd samples/springboot/app && mvn -q test -Dtest=TaskControllerTest`
Expected: FAIL (404s / context has no controller).

- [ ] **Step 4: Write `TaskController.java`**

The NPE is deliberate: when `title` is null but `note` is present, we call `req.title.trim()` with no guard. When both are null we still call it, so an empty POST also 500s — acceptable. Duplicate title is left unchecked so JPA raises the constraint violation.

```java
package com.dbops.todo;

import java.util.List;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api")
public class TaskController {
    private static final Logger log = LoggerFactory.getLogger(TaskController.class);
    private final TaskRepository repo;

    public TaskController(TaskRepository repo) {
        this.repo = repo;
    }

    @GetMapping("/health")
    public Map<String, String> health() {
        return Map.of("status", "UP");
    }

    @GetMapping("/tasks")
    public List<Task> list() {
        return repo.findAll();
    }

    @GetMapping("/tasks/{id}")
    public ResponseEntity<Task> get(@PathVariable Long id) {
        return repo.findById(id).map(ResponseEntity::ok)
                .orElse(ResponseEntity.notFound().build());
    }

    @PostMapping("/tasks")
    public Task create(@RequestBody TaskRequest req) {
        // BUG 1 (NPE): no null-guard on title. A body with note but no title
        // dereferences null here -> NullPointerException -> 500 (ERROR log).
        String normalized = req.title.trim();
        log.info("Creating task title={}", normalized);
        // BUG 2 (constraint): no duplicate-title pre-check. Second identical
        // title raises DataIntegrityViolationException -> 500 (ERROR log).
        return repo.save(new Task(normalized, Boolean.TRUE.equals(req.done), req.note));
    }

    @PutMapping("/tasks/{id}")
    public ResponseEntity<Task> update(@PathVariable Long id, @RequestBody TaskRequest req) {
        return repo.findById(id).map(t -> {
            if (req.title != null) t.setTitle(req.title);
            if (req.done != null) t.setDone(req.done);
            if (req.note != null) t.setNote(req.note);
            return ResponseEntity.ok(repo.save(t));
        }).orElse(ResponseEntity.notFound().build());
    }

    @DeleteMapping("/tasks/{id}")
    public ResponseEntity<Void> delete(@PathVariable Long id) {
        if (!repo.existsById(id)) return ResponseEntity.notFound().build();
        repo.deleteById(id);
        return ResponseEntity.noContent().build();
    }
}
```

- [ ] **Step 5: Run test — expect PASS**

Run: `cd samples/springboot/app && mvn -q test -Dtest=TaskControllerTest`
Expected: all 4 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add samples/springboot/app/src
git commit -m "feat(samples): task CRUD + health, NPE and constraint bugs"
```

---

### Task 4: LeakController — resource-leak bug (bug 3)

**Files:**
- Create: `samples/springboot/app/src/main/java/com/dbops/todo/LeakController.java`
- Test: `samples/springboot/app/src/test/java/com/dbops/todo/LeakControllerTest.java`

**Interfaces:**
- Produces: `GET /api/leak` — each call appends a retained 1 MB buffer to a `static` list (never released) and increments a counter. Logs `WARN` every call ("leaked resource, open handles=N"), escalates to `ERROR` once N crosses a threshold (10). Static accessor `LeakController.openHandles()` for the test.

- [ ] **Step 1: Write the failing test**

```java
package com.dbops.todo;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

class LeakControllerTest {
    @BeforeEach
    void reset() {
        LeakController.reset();
    }

    @Test
    void eachCallLeaksOneHandle() {
        LeakController c = new LeakController();
        c.leak();
        c.leak();
        assertThat(LeakController.openHandles()).isEqualTo(2);
    }

    @Test
    void crossingThresholdIsFlagged() {
        LeakController c = new LeakController();
        for (int i = 0; i < 11; i++) c.leak();
        assertThat(LeakController.openHandles()).isGreaterThan(10);
    }
}
```

- [ ] **Step 2: Run test — expect FAIL (no class)**

Run: `cd samples/springboot/app && mvn -q test -Dtest=LeakControllerTest`
Expected: FAIL to compile (class missing).

- [ ] **Step 3: Write `LeakController.java`**

```java
package com.dbops.todo;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api")
public class LeakController {
    private static final Logger log = LoggerFactory.getLogger(LeakController.class);
    private static final int THRESHOLD = 10;
    // BUG 3 (resource leak): retained buffers, never released. Grows heap over
    // time -> rising memory on host metrics + WARN/ERROR trickle in logs.
    private static final List<byte[]> LEAKED = new ArrayList<>();

    @GetMapping("/leak")
    public Map<String, Object> leak() {
        LEAKED.add(new byte[1024 * 1024]); // 1 MB retained, never freed
        int open = LEAKED.size();
        if (open > THRESHOLD) {
            log.error("Resource leak threshold exceeded, open handles={}", open);
        } else {
            log.warn("Leaked resource, open handles={}", open);
        }
        return Map.of("open_handles", open, "leaked_mb", open);
    }

    public static int openHandles() {
        return LEAKED.size();
    }

    public static void reset() {
        LEAKED.clear();
    }
}
```

- [ ] **Step 4: Run test — expect PASS**

Run: `cd samples/springboot/app && mvn -q test -Dtest=LeakControllerTest`
Expected: PASS.

- [ ] **Step 5: Run full suite**

Run: `cd samples/springboot/app && mvn -q test`
Expected: all tests PASS. This is the deliverable gate for the app.

- [ ] **Step 6: Commit**

```bash
git add samples/springboot/app/src
git commit -m "feat(samples): resource-leak endpoint (bug 3)"
```

---

### Task 5: CDK stack — VPC, NAT, EC2, CloudWatch Agent, Log Group

**Files:**
- Create: `samples/springboot/cdk/cdk.json`
- Create: `samples/springboot/cdk/requirements.txt`
- Create: `samples/springboot/cdk/app.py`
- Create: `samples/springboot/cdk/springboot_apm_stack.py`

**Interfaces:**
- Consumes: the built fat jar at `samples/springboot/app/target/todoapp.jar` (packaged as an S3 asset via `aws_s3_assets.Asset`).
- Produces: stack `dbops-dev-springboot-apm` with CfnOutputs `InstanceId`, `LogGroup`, `Region`, `VpcId`. EC2 tagged `Name=dbops-apm-todoapp` (used by the load generator to resolve the private IP in Task 6).

- [ ] **Step 1: Write `cdk.json`**

```json
{
  "app": "python3 app.py"
}
```

- [ ] **Step 2: Write `requirements.txt`**

```
aws-cdk-lib>=2.262.1
constructs>=10.7.1
```

- [ ] **Step 3: Write `app.py`**

Reads account/region from the main config so it always targets the DBOps account. Falls back to CDK env if the import path is unavailable.

```python
import sys
import os

import aws_cdk as cdk

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "cdk"))
from config.settings import Settings  # noqa: E402
from springboot_apm_stack import SpringbootApmStack  # noqa: E402

app = cdk.App()
env = cdk.Environment(account=Settings.ACCOUNT_ID, region=Settings.REGION)
SpringbootApmStack(app, f"dbops-{Settings.ENV}-springboot-apm", env=env)
app.synth()
```

- [ ] **Step 4: Write `springboot_apm_stack.py`**

VPC with 1 NAT, private-egress subnet for the app; EC2 with no public IP, no inbound SG rules; IAM role with SSM + CloudWatch Agent policies; user-data installs Corretto 17, drops the jar from the S3 asset, writes the systemd unit, and installs+configures the CloudWatch Agent (tail `app.log` → Log Group, publish mem/disk; CPU comes from `AWS/EC2` natively). Log Group pre-created with 7-day retention.

```python
import aws_cdk as cdk
from aws_cdk import (
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_logs as logs,
    aws_s3_assets as s3_assets,
)
from constructs import Construct

LOG_GROUP = "/dbops/apm/todoapp"


class SpringbootApmStack(cdk.Stack):
    def __init__(self, scope: Construct, cid: str, **kwargs):
        super().__init__(scope, cid, **kwargs)

        vpc = ec2.Vpc(
            self, "ApmVpc",
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
                ec2.SubnetConfiguration(name="private", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS, cidr_mask=24),
            ],
        )

        log_group = logs.LogGroup(
            self, "AppLogGroup",
            log_group_name=LOG_GROUP,
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        role = iam.Role(
            self, "AppInstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
                iam.ManagedPolicy.from_aws_managed_policy_name("CloudWatchAgentServerPolicy"),
            ],
        )

        sg = ec2.SecurityGroup(
            self, "AppSg", vpc=vpc, allow_all_outbound=True,
            description="todoapp — egress only, no inbound",
        )
        # No inbound rules. Load generator SG is granted ingress in Task 6.

        jar_asset = s3_assets.Asset(
            self, "TodoJar",
            path="../app/target/todoapp.jar",
        )
        jar_asset.grant_read(role)

        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            "set -euxo pipefail",
            "dnf install -y java-17-amazon-corretto amazon-cloudwatch-agent",
            "mkdir -p /var/log/todoapp /opt/todoapp",
            f"aws s3 cp s3://{jar_asset.s3_bucket_name}/{jar_asset.s3_object_key} /opt/todoapp/todoapp.jar",
            # systemd unit
            "cat >/etc/systemd/system/todoapp.service <<'EOF'\n"
            "[Unit]\nDescription=todoapp\nAfter=network.target\n\n"
            "[Service]\nExecStart=/usr/bin/java -jar /opt/todoapp/todoapp.jar\n"
            "Environment=LOG_DIR=/var/log/todoapp\nRestart=always\nUser=root\n\n"
            "[Install]\nWantedBy=multi-user.target\nEOF",
            "systemctl daemon-reload",
            "systemctl enable --now todoapp",
            # CloudWatch Agent config
            "cat >/opt/aws/amazon-cloudwatch-agent/etc/config.json <<'EOF'\n"
            "{\n"
            '  "agent": {"metrics_collection_interval": 60},\n'
            '  "logs": {"logs_collected": {"files": {"collect_list": [\n'
            f'    {{"file_path": "/var/log/todoapp/app.log", "log_group_name": "{LOG_GROUP}", "log_stream_name": "{{instance_id}}"}}\n'
            "  ]}}},\n"
            '  "metrics": {"append_dimensions": {"InstanceId": "${aws:InstanceId}"},\n'
            '    "metrics_collected": {\n'
            '      "mem": {"measurement": ["mem_used_percent"]},\n'
            '      "disk": {"measurement": ["disk_used_percent"], "resources": ["/"]}\n'
            "    }}\n"
            "}\nEOF",
            "/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl "
            "-a fetch-config -m ec2 -s -c file:/opt/aws/amazon-cloudwatch-agent/etc/config.json",
        )

        instance = ec2.Instance(
            self, "AppInstance",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            instance_type=ec2.InstanceType("t3.small"),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(),
            role=role,
            security_group=sg,
            user_data=user_data,
        )
        cdk.Tags.of(instance).add("Name", "dbops-apm-todoapp")
        log_group.grant_write(role)

        self.vpc = vpc
        self.app_sg = sg
        self.instance = instance

        cdk.CfnOutput(self, "InstanceId", value=instance.instance_id)
        cdk.CfnOutput(self, "LogGroup", value=LOG_GROUP)
        cdk.CfnOutput(self, "Region", value=self.region)
        cdk.CfnOutput(self, "VpcId", value=vpc.vpc_id)
```

- [ ] **Step 5: Synth to verify (jar must exist first)**

Run:
```bash
cd samples/springboot/app && mvn -q package -DskipTests && cd ../cdk && \
pip install -r requirements.txt -q && cdk synth dbops-dev-springboot-apm >/dev/null && echo SYNTH_OK
```
Expected: `SYNTH_OK`. (If Maven/CDK CLI unavailable here, note it; the templates are still committed for the deploy host.)

- [ ] **Step 6: Commit**

```bash
git add samples/springboot/cdk
git commit -m "feat(samples): CDK stack — private EC2 + CW Agent + log group"
```

---

### Task 6: Load-generator Lambda + schedule + ingress grant

**Files:**
- Modify: `samples/springboot/cdk/springboot_apm_stack.py` (append load generator)

**Interfaces:**
- Consumes: `self.vpc`, `self.app_sg` from Task 5; EC2 tag `Name=dbops-apm-todoapp`.
- Produces: a Python Lambda in the private subnets, EventBridge `rate(2 minutes)`, allowed inbound to the app SG on 8080. Resolves the app private IP via `ec2:DescribeInstances` (tag filter), sends ~mostly-200 traffic plus a low rate of the three bug triggers.

- [ ] **Step 1: Append load-generator construct to `springboot_apm_stack.py`**

Add these imports at the top:

```python
from aws_cdk import (
    aws_lambda as lambda_,
    aws_events as events,
    aws_events_targets as targets,
)
```

Append at the end of `__init__` (uses `self.vpc`, `self.app_sg`):

```python
        self.app_sg.add_ingress_rule(
            peer=ec2.Peer.security_group_id(self.app_sg.security_group_id),
            connection=ec2.Port.tcp(8080),
            description="load-gen -> app (same SG)",
        )

        load_gen = lambda_.Function(
            self, "LoadGen",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_inline(LOAD_GEN_CODE),
            timeout=cdk.Duration.minutes(2),
            memory_size=256,
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[self.app_sg],
            environment={"APP_TAG": "dbops-apm-todoapp", "APP_PORT": "8080"},
        )
        load_gen.add_to_role_policy(iam.PolicyStatement(
            actions=["ec2:DescribeInstances"], resources=["*"],
        ))
        events.Rule(
            self, "LoadSchedule",
            schedule=events.Schedule.rate(cdk.Duration.minutes(2)),
            targets=[targets.LambdaFunction(load_gen)],
        )
```

- [ ] **Step 2: Add `LOAD_GEN_CODE` at the module bottom**

Uses only the Python stdlib (`urllib`) + boto3 (both present in the Lambda runtime), so no packaging needed. ~20 healthy calls, then 1 of each bug per invocation.

```python
LOAD_GEN_CODE = r'''
import json, os, urllib.request, urllib.error
import boto3

def _app_ip():
    ec2 = boto3.client("ec2")
    r = ec2.describe_instances(Filters=[
        {"Name": "tag:Name", "Values": [os.environ["APP_TAG"]]},
        {"Name": "instance-state-name", "Values": ["running"]},
    ])
    for res in r["Reservations"]:
        for inst in res["Instances"]:
            ip = inst.get("PrivateIpAddress")
            if ip:
                return ip
    return None

def _call(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return -1

def handler(event, context):
    ip = _app_ip()
    if not ip:
        return {"error": "app instance not found"}
    base = f"http://{ip}:{os.environ.get('APP_PORT','8080')}/api"
    counts = {}
    def bump(k): counts[k] = counts.get(k, 0) + 1
    # healthy traffic
    for i in range(20):
        bump(f"health_{_call('GET', base + '/health')}")
    for i in range(10):
        bump(f"create_{_call('POST', base + '/tasks', {'title': f'task-{context.aws_request_id}-{i}'})}")
        bump(f"list_{_call('GET', base + '/tasks')}")
    # bug 1: NPE (note, no title)
    bump(f"npe_{_call('POST', base + '/tasks', {'note': 'orphan'})}")
    # bug 2: duplicate title -> constraint
    _call('POST', base + '/tasks', {'title': 'dup-fixed'})
    bump(f"dup_{_call('POST', base + '/tasks', {'title': 'dup-fixed'})}")
    # bug 3: resource leak
    bump(f"leak_{_call('GET', base + '/leak')}")
    return counts
'''
```

- [ ] **Step 3: Re-synth to verify**

Run: `cd samples/springboot/cdk && cdk synth dbops-dev-springboot-apm >/dev/null && echo SYNTH_OK`
Expected: `SYNTH_OK`.

- [ ] **Step 4: Commit**

```bash
git add samples/springboot/cdk/springboot_apm_stack.py
git commit -m "feat(samples): VPC-internal load generator + 2-min schedule"
```

---

### Task 7: README — deploy + APM target registration walkthrough

**Files:**
- Create: `samples/springboot/README.md`

**Interfaces:** none (docs).

- [ ] **Step 1: Write `README.md`** (Korean, 해요체) covering:
  - What this is (APM test target), cost warning (1 NAT ~$32/mo + t3.small).
  - Prereqs: Java 17 + Maven on the build host; CDK CLI ≥ 2.1134.0; deploying to account `571850511781` / `us-east-1`.
  - Deploy: `cd samples/springboot/app && mvn -q package -DskipTests` then `cd ../cdk && pip install -r requirements.txt && cdk deploy dbops-dev-springboot-apm`.
  - Verify the app: `aws ssm start-session --target <InstanceId>` → `systemctl status todoapp`, `journalctl -u todoapp -n 50`, `tail /var/log/todoapp/app.log`.
  - Register the APM target in DBOps `/apm`: paste `InstanceId`, `LogGroup=/dbops/apm/todoapp`, `region=us-east-1`, **leave `spoke_role_arn` blank** (same account). Explain why (local session, `_session_for` with empty role).
  - What to expect: log search (default ERROR+WARN) returns NPE stack traces, constraint violations, and leak WARN/ERROR lines; host CPU/mem cards populate; **latency/APM-metric cards stay empty (no ADOT — by design)**.
  - The three bugs and their triggers (table).
  - Deploy-time IAM caveat: if `/apm` log search returns AccessDenied, the APM Lambda role in `cdk/stacks/agent_stack.py` needs same-account CloudWatch read (`logs:StartQuery`/`GetQueryResults`/`FilterLogEvents`, `cloudwatch:GetMetricData`). Follow-up, outside this sample.
  - Cleanup: `cd samples/springboot/cdk && cdk destroy dbops-dev-springboot-apm` (self-contained own VPC, no ordering dependency with main stacks).

- [ ] **Step 2: Commit**

```bash
git add samples/springboot/README.md
git commit -m "docs(samples): Spring Boot APM target deploy + registration guide"
```

---

## Self-Review

**Spec coverage:**
- §3 layout → Tasks 1–7 create exactly the listed files. ✓
- §4 domain/routes/logging/bugs → Tasks 1–4 (JSON logging swap from plain-text is documented in Global Constraints with rationale: collector `stats count() by level` needs a `level` field). ✓
- §5 VPC/NAT/EC2/CW Agent/load-gen/outputs → Tasks 5–6. ✓
- §6 integration flow → Task 7 README. ✓
- §7 testing → app `mvn test` in Tasks 1–4; `cdk synth` in Tasks 5–6; post-deploy browser check is a manual gate in the README + executed at end. ✓
- §8 cost/cleanup → Task 7. ✓

**Placeholder scan:** No TBD/TODO; all code blocks are complete. The only deferred item is the optional same-account IAM grant on the APM Lambda, explicitly scoped OUT in both spec §6 and Task 7 (verified at deploy time, not guessed). ✓

**Type consistency:** `Task(title, done, note)` ctor used consistently; `TaskRequest` public fields (`title`/`done`/`note`) match controller usage; `LeakController.openHandles()`/`reset()`/`leak()` match the test; CfnOutput names (`InstanceId`/`LogGroup`/`Region`/`VpcId`) and the EC2 `Name` tag (`dbops-apm-todoapp`) match the load generator's `APP_TAG`. Log Group `/dbops/apm/todoapp` consistent across stack, agent config, and README. ✓
