package com.example.snmppolling;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableScheduling; // Enable scheduling

@SpringBootApplication
@EnableScheduling // This enables Spring's scheduled tasks
public class SnmpPollingApplication {

    public static void main(String[] args) {
        SpringApplication.run(SnmpPollingApplication.class, args);
    }

}