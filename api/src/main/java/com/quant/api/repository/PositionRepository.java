package com.quant.api.repository;

import com.quant.api.domain.Position;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

public interface PositionRepository extends JpaRepository<Position, Long> {
    List<Position> findByModeAndQuantityGreaterThanOrderByUpdatedAtDesc(String mode, Integer quantity);
}
