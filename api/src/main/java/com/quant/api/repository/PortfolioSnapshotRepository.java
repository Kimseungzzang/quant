package com.quant.api.repository;

import com.quant.api.domain.PortfolioSnapshot;
import org.springframework.data.jpa.repository.JpaRepository;

import java.time.LocalDate;
import java.util.List;
import java.util.Optional;

public interface PortfolioSnapshotRepository extends JpaRepository<PortfolioSnapshot, Long> {

    List<PortfolioSnapshot> findByModeOrderBySnapshotDateAsc(String mode);

    List<PortfolioSnapshot> findByModeAndSnapshotDateGreaterThanEqualOrderBySnapshotDateAsc(
        String mode, LocalDate since
    );

    Optional<PortfolioSnapshot> findBySnapshotDateAndMode(LocalDate date, String mode);
}
