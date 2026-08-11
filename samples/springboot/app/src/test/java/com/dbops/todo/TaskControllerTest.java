package com.dbops.todo;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

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
        // Unique title + membership assertion: @SpringBootTest reuses the H2
        // context across tests, so we must not assume ordering or an empty table.
        mvc.perform(post("/api/tasks").contentType(MediaType.APPLICATION_JSON)
                .content("{\"title\":\"write plan\",\"done\":false}"))
           .andExpect(status().isOk());
        mvc.perform(get("/api/tasks")).andExpect(status().isOk())
           .andExpect(jsonPath("$[?(@.title == 'write plan')]").exists());
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
