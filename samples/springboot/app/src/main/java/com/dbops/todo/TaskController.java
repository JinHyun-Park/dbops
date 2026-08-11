package com.dbops.todo;

import java.util.List;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

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
        // BUG 2 (constraint): no duplicate-title pre-check. A second identical
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
