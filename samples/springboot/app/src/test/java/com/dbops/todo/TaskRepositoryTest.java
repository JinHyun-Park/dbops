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
